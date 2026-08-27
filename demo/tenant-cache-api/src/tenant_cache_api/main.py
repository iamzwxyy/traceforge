from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException

from tenant_cache_api.cache import TenantTTLCache

app = FastAPI(title="Tenant Cache API")
cache: TenantTTLCache[dict[str, str]] = TenantTTLCache()

PROFILES = {
    ("acme", "42"): {"id": "42", "name": "Ada @ Acme"},
    ("globex", "42"): {"id": "42", "name": "Grace @ Globex"},
}


@app.get("/profiles/{profile_id}")
def get_profile(profile_id: str, x_tenant_id: str = Header()) -> dict[str, str]:
    def load() -> dict[str, str]:
        try:
            return PROFILES[(x_tenant_id, profile_id)]
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Profile not found") from exc

    return cache.get_or_load(x_tenant_id, profile_id, load)
