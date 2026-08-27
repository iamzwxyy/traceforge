from tenant_cache_api.cache import TenantTTLCache


def test_cache_hit_avoids_duplicate_load() -> None:
    cache: TenantTTLCache[str] = TenantTTLCache(clock=lambda: 10)
    loads = 0

    def load() -> str:
        nonlocal loads
        loads += 1
        return "profile"

    assert cache.get_or_load("acme", "42", load) == "profile"
    assert cache.get_or_load("acme", "42", load) == "profile"
    assert loads == 1


def test_expired_entry_is_reloaded() -> None:
    now = 10.0
    cache: TenantTTLCache[int] = TenantTTLCache(clock=lambda: now)
    values = iter([1, 2])

    assert cache.get_or_load("acme", "42", lambda: next(values), ttl_seconds=5) == 1
    now = 16.0
    assert cache.get_or_load("acme", "42", lambda: next(values), ttl_seconds=5) == 2
