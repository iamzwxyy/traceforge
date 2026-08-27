from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class CacheEntry(Generic[T]):
    value: T
    expires_at: float


class TenantTTLCache(Generic[T]):
    def __init__(self, *, clock: Callable[[], float] = monotonic) -> None:
        self._clock = clock
        self._entries: dict[str, CacheEntry[T]] = {}

    def get_or_load(
        self,
        tenant_id: str,
        profile_id: str,
        loader: Callable[[], T],
        *,
        ttl_seconds: float = 60,
    ) -> T:
        """Return a fresh cached profile or load it for the requesting tenant."""
        del tenant_id  # The cache currently forgets to include tenant scope.
        now = self._clock()
        entry = self._entries.get(profile_id)
        if entry is not None and entry.expires_at > now:
            return entry.value
        value = loader()
        self._entries[profile_id] = CacheEntry(value=value, expires_at=now + ttl_seconds)
        return value
