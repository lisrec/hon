"""Real aiohttp sockets, bounded to loopback; inherited real HA fixtures."""
import socket
import pytest
from aiohttp import web

@pytest.fixture(autouse=True)
def http(monkeypatch):
    # Override parent aioresponses fixture: this suite exercises real sockets.
    original = socket.socket.connect
    def guarded(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            assert address[0] in ("127.0.0.1", "::1"), f"Non-loopback denied: {address}"
        return original(sock, address)
    monkeypatch.setattr(socket.socket, "connect", guarded)

@pytest.fixture
async def server(monkeypatch):
    import custom_components.hon.hon as transport
    runners = []
    async def start(handler, ssl_context=None):
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", handler)
        # Do not copy the credential-bearing authorize URL into access logs.
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ssl_context)
        await site.start()
        runners.append(runner)
        port = site._server.sockets[0].getsockname()[1]
        url = f"{'https' if ssl_context else 'http'}://127.0.0.1:{port}"
        monkeypatch.setattr(transport, "API_URL", url)
        return url
    yield start
    for runner in runners:
        await runner.cleanup()
