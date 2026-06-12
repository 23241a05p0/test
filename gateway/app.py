"""Asynchronous API Gateway (aiohttp).

Every incoming request passes through `rate_limit_middleware`:
  1. figure out *who* the client is (API key header, else IP),
  2. ask the RateLimiter if they have a token,
  3. reject with 429 if not, otherwise proxy the request to the upstream service.

asyncio + aiohttp let one process juggle thousands of concurrent requests by
awaiting network I/O instead of blocking a thread per request.
"""

import os

from aiohttp import web, ClientSession, ClientTimeout

from rate_limiter import RateLimiter

UPSTREAM_URL = os.getenv("UPSTREAM_URL", "http://localhost:8000")
PORT = int(os.getenv("PORT", "8080"))

# Headers that must not be copied verbatim when proxying (hop-by-hop).
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
}


def client_id(request: web.Request) -> str:
    """Identify the caller: prefer an API key, fall back to the source IP."""
    api_key = request.headers.get("X-API-Key")
    if api_key:
        return f"key:{api_key}"
    return f"ip:{request.remote or 'unknown'}"


@web.middleware
async def rate_limit_middleware(request: web.Request, handler):
    # /health is exempt so monitoring never gets throttled.
    if request.path == "/health":
        return await handler(request)

    limiter: RateLimiter = request.app["limiter"]
    if not await limiter.allow(client_id(request)):
        return web.json_response(
            {"error": "rate_limited", "message": "Too Many Requests"},
            status=429,
        )
    return await handler(request)


async def proxy(request: web.Request) -> web.Response:
    """Forward the request to the upstream and relay its response back."""
    session: ClientSession = request.app["session"]
    target = f"{UPSTREAM_URL}{request.rel_url}"
    body = await request.read()

    # Don't forward the original Host header; let aiohttp set it for the upstream.
    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}

    async with session.request(
        request.method, target, headers=fwd_headers, data=body,
        allow_redirects=False,
    ) as upstream_resp:
        raw = await upstream_resp.read()
        resp_headers = {
            k: v for k, v in upstream_resp.headers.items()
            if k.lower() not in _HOP_BY_HOP
        }
        return web.Response(
            body=raw, status=upstream_resp.status, headers=resp_headers,
        )


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def on_startup(app: web.Application) -> None:
    app["limiter"] = RateLimiter()
    await app["limiter"].connect()
    # One shared session = connection pooling = high throughput.
    app["session"] = ClientSession(timeout=ClientTimeout(total=10))


async def on_cleanup(app: web.Application) -> None:
    await app["session"].close()
    await app["limiter"].close()


def make_app() -> web.Application:
    app = web.Application(middlewares=[rate_limit_middleware])
    app.router.add_get("/health", health)
    # Catch-all: every other path/method is proxied upstream.
    app.router.add_route("*", "/{tail:.*}", proxy)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), port=PORT)
