"""Quick concurrent burst against the gateway (no k6 needed).

Unlike a curl loop (one slow process per request), this fires N requests
*concurrently* from one asyncio event loop, so they arrive fast enough to
empty the token bucket and trigger 429s.

Usage:  python load/burst.py [N] [URL]
"""

import asyncio
import sys
from collections import Counter

import aiohttp

N = int(sys.argv[1]) if len(sys.argv) > 1 else 500
URL = sys.argv[2] if len(sys.argv) > 2 else "http://localhost:8080/"


async def one(session: aiohttp.ClientSession) -> int:
    async with session.get(URL) as resp:
        return resp.status


async def main() -> None:
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(*(one(session) for _ in range(N)))
    counts = Counter(results)
    print(f"{N} concurrent requests -> {dict(counts)}")
    print(f"  allowed (200): {counts.get(200, 0)}")
    print(f"  throttled (429): {counts.get(429, 0)}")


if __name__ == "__main__":
    asyncio.run(main())
