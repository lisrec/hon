"""Socket/cancellation verification and explicit-login single-flight regression.

Only the F12 explicit-login case adds a separately recorded RED/GREEN fix.
"""
import asyncio
import traceback
from types import SimpleNamespace
import aiohttp
import pytest
from aiohttp import web
from homeassistant.exceptions import HomeAssistantError, ConfigEntryAuthFailed

DEVICE = SimpleNamespace(mac_address="00:00:00:00:00:01", appliance_type="AC", commands_options={})

@pytest.mark.parametrize("operation", ["authorize", "context", "async_set", "send_command"])
@pytest.mark.parametrize("failure", ["timeout", "disconnect"])
async def test_socket_failures_are_secret_safe(server, connection, monkeypatch, caplog, operation, failure):
    release = asyncio.Event()
    paths = []
    async def handler(request):
        paths.append(request.path)
        if failure == "timeout":
            await release.wait()
        else:
            request.transport.close()
        return web.json_response({})
    url = await server(handler)
    monkeypatch.setattr(connection._session, "_timeout", aiohttp.ClientTimeout(total=0.05))
    try:
        with pytest.raises(HomeAssistantError) as caught:
            if operation == "authorize":
                await connection.async_authorize()
            elif operation == "context":
                await connection.async_get_context(DEVICE)
            elif operation == "async_set":
                await connection.async_set(DEVICE.mac_address, "AC", {})
            else:
                await connection.send_command(DEVICE, "settings", {}, {})
        assert not isinstance(caught.value, ConfigEntryAuthFailed)
        rendered = "".join(traceback.format_exception(caught.value)) + caplog.text
        assert url not in rendered
        assert "password=" not in rendered
        assert "offline-only" not in rendered
        if operation in ("async_set", "send_command"):
            assert paths == ["/commands/v1/send"]
    finally:
        release.set()

@pytest.mark.parametrize("status", [401, 503])
async def test_concurrent_failed_refresh_is_one_attempt(server, connection, status):
    paths = []
    async def handler(request):
        paths.append(request.path)
        await asyncio.sleep(0)
        return web.json_response({"error": "denied"}, status=status)
    await server(handler)
    connection._start_time = 0
    results = await asyncio.wait_for(asyncio.gather(*(connection.async_get_context(DEVICE) for _ in range(8)), return_exceptions=True), 2)
    assert all(isinstance(result, HomeAssistantError) for result in results)
    assert all(isinstance(result, ConfigEntryAuthFailed) == (status == 401) for result in results)
    assert paths == ["/ciam/authorize"]

async def test_F12_concurrent_explicit_authorize_is_single_flight(server, connection):
    paths = []
    async def handler(request):
        paths.append(request.path)
        if request.path == "/ciam/authorize":
            return web.json_response({"session_id": "synthetic-session"})
        if request.path == "/ciam/token":
            return web.json_response({"tokens": {"id_token": "synthetic-id", "cognito_token": "synthetic-cognito"}})
        return web.json_response({"modules": {"applianceList": {"payload": {"appliances": []}}}})
    await server(handler)
    results = await asyncio.wait_for(asyncio.gather(*(connection.async_authorize() for _ in range(8))), 3)
    assert results == [True] * 8
    assert paths == ["/ciam/authorize", "/ciam/token", "/unified-api/v1/view/appliance-list"]

async def test_cancelled_refresh_releases_lock_for_next_caller(server, connection):
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        if request.path == "/ciam/authorize":
            entered.set()
            await release.wait()
            return web.json_response({"session_id": "synthetic-session"})
        if request.path == "/ciam/token":
            return web.json_response({"tokens": {"id_token": "synthetic-id", "cognito_token": "synthetic-cognito"}})
        if request.path == "/commands/v1/context":
            return web.json_response({"payload": {"shadow": {"parameters": {"tempSel": {"parNewVal": "21"}}}}})
        return web.json_response({"modules": {"applianceList": {"payload": {"appliances": []}}}})
    await server(handler)
    connection._start_time = 0
    task = asyncio.create_task(connection.async_get_context(DEVICE))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    assert not connection._session_lock.locked()
    result = await asyncio.wait_for(connection.async_get_context(DEVICE), 2)
    assert result["shadow"]["parameters"]["tempSel"]["parNewVal"] == "21"
