"""Headers both web servers (voice/token_server.py, voice/session_server.py) put on every answer.

aiohttp names itself and Python with their versions in a Server header, which only helps someone probing the server
(black-box probe 5, 2026-10-15); the servers say "Bee Call" instead.
"""

from aiohttp import web

SERVER_NAME = "Bee Call"


async def plain_server_name(request: web.Request, response: web.StreamResponse) -> None:
    response.headers["Server"] = SERVER_NAME  # aiohttp only sets its own when none is there
