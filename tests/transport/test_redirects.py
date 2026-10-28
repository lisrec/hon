"""R1 regressions adapted from the independent redirect characterization probes."""
import socket
import ssl
import traceback
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from homeassistant.exceptions import HomeAssistantError

import custom_components.hon.hon as hon

DEVICE = SimpleNamespace(
    mac_address="00:00:00:00:00:01", appliance_type="AC", commands_options={}
)


class LoopbackResolver(aiohttp.abc.AbstractResolver):
    """Resolve only the fixture certificate hostname to the guarded loopback."""

    async def resolve(self, host, port=0, family=0):
        assert host == "untrusted-audit-loopback.invalid"
        return [{"hostname": host, "host": "127.0.0.1", "port": port,
                 "family": socket.AF_INET, "proto": 0, "flags": 0}]

    async def close(self):
        pass


async def start_source(server, connection, monkeypatch, handler, scheme):
    if scheme == "http":
        return await server(handler)
    here = Path(__file__).parent
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(here / "loopback.crt", here / "loopback.key")
    url = await server(handler, context)
    # Trust the fixture issuer without disabling chain or hostname verification.
    client_context = ssl.create_default_context(cafile=str(here / "loopback.crt"))
    await connection._session.close()
    connection._session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(
        ssl=client_context, resolver=LoopbackResolver()
    ))
    url = url.replace("127.0.0.1", "untrusted-audit-loopback.invalid")
    monkeypatch.setattr(hon, "API_URL", url)
    return url


@pytest.mark.parametrize("operation", ["async_set", "send_command"])
@pytest.mark.parametrize("source_scheme", ["http", "https"])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_command_redirect_rejected_without_replay_or_token_disclosure(
    server, connection, monkeypatch, caplog, operation, source_scheme, status
):
    source_requests, target_requests = [], []

    async def target(request):
        target_requests.append({"method": request.method,
                                "headers": dict(request.headers),
                                "body": await request.read()})
        return web.json_response({"payload": {"resultCode": "0"}})

    destination = await server(target)

    async def source(request):
        source_requests.append({"method": request.method, "path": request.path,
                                "body": await request.json()})
        return web.Response(status=status, headers={
            "Location": destination + "/redirected-command?secret=SYNTHETIC-LOCATION"
        })

    source_url = await start_source(server, connection, monkeypatch, source, source_scheme)
    connection._id_token = "SYNTHETIC-ID-SECRET"
    connection._cognitoToken = "SYNTHETIC-COGNITO-SECRET"
    with pytest.raises(HomeAssistantError, match="hOn failed to send the command") as caught:
        if operation == "async_set":
            await connection.async_set(DEVICE.mac_address, "AC", {"echoStatus": "0"})
        else:
            await connection.send_command(DEVICE, "settings", {"echoStatus": "0"}, {})
    assert len(source_requests) == 1
    assert source_requests[0]["method"] == "POST"
    assert source_requests[0]["path"] == "/commands/v1/send"
    assert source_requests[0]["body"]["parameters"] == {"echoStatus": "0"}
    assert target_requests == []  # No second request, body replay, or custom bearer headers.
    rendered = "".join(traceback.format_exception(caught.value)) + caplog.text
    for secret in (source_url, destination, "SYNTHETIC-ID-SECRET",
                   "SYNTHETIC-COGNITO-SECRET", "SYNTHETIC-LOCATION"):
        assert secret not in rendered


@pytest.mark.parametrize("source_scheme", ["http", "https"])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_authorize_redirect_rejected_without_credential_disclosure(
    server, connection, monkeypatch, caplog, source_scheme, status
):
    source_requests, target_requests = [], []

    async def target(request):
        target_requests.append(str(request.rel_url))
        return web.json_response({"session_id": "synthetic-session"})

    destination = await server(target)

    async def source(request):
        source_requests.append((request.method, request.path, dict(request.query)))
        # A redirect could explicitly copy the original credential-bearing query.
        location = destination + "/redirected-authorize?" + request.query_string
        return web.Response(status=status, headers={"Location": location})

    source_url = await start_source(server, connection, monkeypatch, source, source_scheme)
    with pytest.raises(HomeAssistantError, match="^hOn request failed$") as caught:
        await connection.async_authorize()
    assert len(source_requests) == 1
    method, path, query = source_requests[0]
    assert (method, path) == ("GET", "/ciam/authorize")
    assert query["username"] == "offline@example.test"
    assert query["password"] == "offline-only"
    assert target_requests == []
    rendered = "".join(traceback.format_exception(caught.value)) + caplog.text
    for secret in (source_url, destination, "offline-only", "password=", "code_challenge="):
        assert secret not in rendered
