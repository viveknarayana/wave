"""Prometheus metrics: QPS, latency histograms, error rates."""

from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

REQUEST_COUNT = Counter(
    "wave_gateway_requests_total",
    "Total requests to the gateway",
    ["method", "path", "status"],
)
REQUEST_LATENCY = Histogram(
    "wave_gateway_request_latency_seconds",
    "Request latency in seconds",
    ["path"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
ERROR_COUNT = Counter(
    "wave_gateway_errors_total",
    "Total errors (4xx/5xx or validation failures)",
    ["path", "reason"],
)


def get_metrics_content_type() -> str:
    return CONTENT_TYPE_LATEST


def get_metrics_bytes() -> bytes:
    return generate_latest()
