"""Async token-bucket rate limiter backed by Redis.

The actual refill+spend logic lives in token_bucket.lua and runs *inside* Redis
so that many concurrent requests can't race each other. This module just loads
that script once and calls it.
"""

import os
import time
from pathlib import Path

import redis.asyncio as redis

# --- Configuration (read from environment, with sensible defaults) ---------
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
RATE = float(os.getenv("RATE", "100"))      # tokens added per second
BURST = float(os.getenv("BURST", "200"))    # max tokens (burst capacity)

_LUA_PATH = Path(__file__).with_name("token_bucket.lua")


class RateLimiter:
    """One instance per process. Call `allow(client_id)` per request."""

    def __init__(self, url: str = REDIS_URL, rate: float = RATE, burst: float = BURST):
        self._url = url
        self._rate = rate
        self._burst = burst
        self._redis: redis.Redis | None = None
        self._script = None  # registered Lua script (uses EVALSHA under the hood)

    async def connect(self) -> None:
        """Open the Redis connection pool and register the Lua script."""
        self._redis = redis.from_url(self._url, decode_responses=True)
        lua = _LUA_PATH.read_text(encoding="utf-8")
        # register_script handles SCRIPT LOAD + EVALSHA (with EVAL fallback).
        self._script = self._redis.register_script(lua)

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()

    async def allow(self, client_id: str, cost: int = 1) -> bool:
        """Return True if the request is allowed, False if rate-limited."""
        now = time.time()
        result = await self._script(
            keys=[f"rl:{client_id}"],
            args=[self._rate, self._burst, now, cost],
        )
        return int(result) == 1


# --- Tiny self-test: run `python -m rate_limiter` with Redis up ------------
# Spends tokens in a tight loop and prints when the bucket runs dry.
if __name__ == "__main__":
    import asyncio

    async def _demo() -> None:
        rl = RateLimiter(rate=5, burst=10)  # 10 burst, refilling 5/sec
        await rl.connect()
        try:
            allowed = denied = 0
            for i in range(20):
                if await rl.allow("demo-client"):
                    allowed += 1
                else:
                    denied += 1
            print(f"out of 20 instant requests: {allowed} allowed, {denied} denied")
            print("waiting 1s for refill...")
            await asyncio.sleep(1)
            print("after refill, allowed again:", await rl.allow("demo-client"))
        finally:
            await rl.close()

    asyncio.run(_demo())
