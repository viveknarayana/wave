"""Prometheus metrics: QPS, latency histograms, error rates, and SLO signals."""

from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST

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

# Phase 7 metrics for tiered SLOs and autoscaling.
REQUESTS_BY_TIER = Counter(
    "wave_requests_total",
    "Requests by tenant tier and final status class",
    ["tenant_tier", "status"],
)
REQUEST_LATENCY_MS_BY_TIER = Histogram(
    "wave_request_latency_ms",
    "End-to-end request latency in milliseconds by tenant tier",
    ["tenant_tier", "status"],
    buckets=(25, 50, 100, 250, 500, 750, 1000, 1500, 2000, 3000, 5000, 10000),
)
ADMISSION_REJECTIONS = Counter(
    "wave_admission_rejections_total",
    "Requests rejected by admission control",
    ["tenant_tier", "reason"],
)
QUEUE_DEPTH = Gauge(
    "wave_queue_depth",
    "Current in-memory queue depth in the gateway",
)
INFLIGHT_REQUESTS = Gauge(
    "wave_inflight_requests",
    "Current in-flight requests in the gateway",
)


def get_metrics_content_type() -> str:
    return CONTENT_TYPE_LATEST


def get_metrics_bytes() -> bytes:
    return generate_latest()
