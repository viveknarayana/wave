"""
Priority-aware batching: favor premium requests over free before calling workers.

Note: This shapes request ordering and short wait windows at the gateway.
Actual low-level batching is still handled by vLLM inside the worker.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class BatchRequest:
    worker_url: str
    body: Dict[str, Any]
    priority: str  # "premium" or "free"/"standard"
    created_at: float = field(default_factory=lambda: time.perf_counter())
    future: asyncio.Future = field(default_factory=asyncio.Future)


class PriorityBatcher:
    """
    Simple in-memory batcher:
    - Queue incoming non-streaming requests.
    - Every small interval, build a batch where premium requests are handled first.
    - For now, each request still results in its own worker call; prioritization is
      about ordering and wait time, not merging payloads.
    """

    def __init__(
        self,
        call_worker,
        max_batch_size: int = 16,
        max_wait_ms: float = 10.0,
    ) -> None:
        self._call_worker = call_worker
        self._queue: asyncio.Queue[BatchRequest] = asyncio.Queue()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._max_batch_size = max_batch_size
        self._max_wait_ms = max_wait_ms
        self._inflight = 0

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    @property
    def inflight_requests(self) -> int:
        return self._inflight

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run(), name="wave-priority-batcher")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def enqueue(self, req: BatchRequest) -> Dict[str, Any]:
        """
        Enqueue a request and wait for its worker response (JSON dict).
        """
        await self._queue.put(req)
        return await req.future

    async def _run(self) -> None:
        while self._running:
            try:
                first = await self._queue.get()
            except asyncio.CancelledError:
                break

            batch: List[BatchRequest] = [first]
            start = time.perf_counter()

            # Small window to accumulate more requests into this batch.
            while len(batch) < self._max_batch_size:
                remaining = self._max_wait_ms / 1000.0 - (time.perf_counter() - start)
                if remaining <= 0:
                    break
                try:
                    nxt = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                    batch.append(nxt)
                except asyncio.TimeoutError:
                    break

            # Prioritize premium over others.
            batch.sort(key=lambda r: 0 if r.priority == "premium" else 1)

            # Dispatch all in this batch concurrently; each still maps to its own worker call.
            await self._dispatch_batch(batch)

    async def _dispatch_batch(self, batch: List[BatchRequest]) -> None:
        async def handle_one(r: BatchRequest) -> None:
            if r.future.done():
                return
            self._inflight += 1
            try:
                raw = await self._call_worker(r.worker_url, r.body)
                r.future.set_result(raw)
            except Exception as exc:  # noqa: BLE001
                if not r.future.done():
                    r.future.set_exception(exc)
            finally:
                self._inflight = max(0, self._inflight - 1)

        await asyncio.gather(*(handle_one(r) for r in batch), return_exceptions=True)

