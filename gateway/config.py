"""Tenant and gateway config (Phase 1: in-memory; Phase 8: full tenant config)."""
from __future__ import annotations

# Tenant config lookup: tenant_id -> limits and allowed models
# Later: load from YAML; for now a simple dict.
TENANTS: dict[str, dict] = {
    "free": {
        "max_context": 2_000,
        "models": ["tiny", "phi-3-mini", "Qwen/Qwen2-0.5B-Instruct"],
        "rate_limit_per_min": 10,
    },
    "acme": {
        "max_context": 8_192,
        "models": ["Qwen/Qwen2-0.5B-Instruct"],
        "rate_limit_per_min": 100,
    },
    "premium": {
        "max_context": 32_000,
        "models": ["all"],
        "rate_limit_per_min": 1000,
    },
}

DEFAULT_TENANT = "free"


def get_tenant_config(tenant_id: str | None) -> dict:
    """Return tenant config for validation. Uses default tenant if None or unknown."""
    if not tenant_id or tenant_id not in TENANTS:
        return TENANTS[DEFAULT_TENANT].copy()
    return TENANTS[tenant_id].copy()
