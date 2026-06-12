"""Tiny upstream service the gateway proxies to.

It does nothing interesting on purpose — it just proves the gateway really
forwards traffic to a separate backend instead of answering itself.
"""

import os

from aiohttp import web

PORT = int(os.getenv("PORT", "8000"))


async def echo(request: web.Request) -> web.Response:
    return web.json_response({
        "ok": True,
        "method": request.method,
        "path": request.path,
    })


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", echo)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), port=PORT)
