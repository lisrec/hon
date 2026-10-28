"""Desired transport behavior using synthetic data and actual local HTTP."""
import asyncio
import ssl
import traceback
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from aiohttp import web
from homeassistant.exceptions import HomeAssistantError, ConfigEntryAuthFailed, ConfigEntryNotReady
import custom_components.hon as integration
from custom_components.hon.hon import HonConnection
from custom_components.hon.config_flow import HonFlowHandler

DEVICE = SimpleNamespace(mac_address="00:00:00:00:00:01", appliance_type="AC", commands_options={})

def auth_reply(request):
    if request.path == "/ciam/authorize":
        return web.json_response({"session_id": "synthetic-session"})
    if request.path == "/ciam/token":
        return web.json_response({"tokens": {"id_token": "synthetic-id", "cognito_token": "synthetic-cognito"}})
    return web.json_response({"modules": {"applianceList": {"payload": {"appliances": []}}}})

async def test_F01_untrusted_tls_rejected_before_credentials(server, connection):
    observed = []
    async def handler(request):
        observed.append(request.path)
        return auth_reply(request)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    here = Path(__file__).parent
    context.load_cert_chain(here / "loopback.crt", here / "loopback.key")
    await server(handler, context)
    # Error translation may wrap TLS failure, but the peer must receive nothing.
    with pytest.raises((aiohttp.ClientError, HomeAssistantError)):
        await connection.async_authorize()
    assert observed == []

@pytest.mark.parametrize("body,content_type", [("secret response", "text/html"), ("{", "application/json"), ("[]", "application/json")])
async def test_F02_authorize_errors_are_secret_safe(server, connection, caplog, body, content_type):
    async def handler(request):
        return web.Response(text=body, content_type=content_type)
    url = await server(handler)
    with pytest.raises(HomeAssistantError) as caught:
        await connection.async_authorize()
    rendered = "".join(traceback.format_exception(caught.value)) + caplog.text
    assert "offline-only" not in rendered
    assert "password=" not in rendered
    assert url not in rendered
    assert "secret response" not in rendered

@pytest.mark.parametrize("stage", ["/ciam/token", "/unified-api/v1/view/appliance-list"])
async def test_F02_partial_auth_bodies_never_logged(server, connection, caplog, stage):
    async def handler(request):
        if request.path == stage:
            return web.json_response({"tokens": {"id_token": "SYNTHETIC-BEARER-SECRET"}})
        return auth_reply(request)
    url = await server(handler)
    caplog.set_level("DEBUG", logger="custom_components.hon")
    with pytest.raises(HomeAssistantError) as caught:
        await connection.async_authorize()
    rendered = "".join(traceback.format_exception(caught.value)) + caplog.text
    assert "SYNTHETIC-BEARER-SECRET" not in rendered
    assert url not in rendered

async def send(connection, operation, appliance_type="AC"):
    if operation == "async_set":
        return await connection.async_set(DEVICE.mac_address, appliance_type, {})
    device = SimpleNamespace(**{**vars(DEVICE), "appliance_type": appliance_type})
    return await connection.send_command(device, "settings", {}, {})

@pytest.mark.parametrize("operation", ["async_set", "send_command"])
@pytest.mark.parametrize("appliance_type,quiet", [("AC", False), ("AC", True), ("WM", True)])
async def test_F03_rejection_always_raises(server, connection, entry, hass, operation, appliance_type, quiet):
    hass.config_entries.async_update_entry(entry, options={"quiet_ac_commands": quiet})
    requests = []
    async def handler(request):
        requests.append(await request.json())
        return web.json_response({"payload": {"resultCode": "1", "secret": "SYNTHETIC-RESPONSE-SECRET"}})
    await server(handler)
    with pytest.raises(HomeAssistantError, match="hOn rejected"):
        await send(connection, operation, appliance_type)
    assert len(requests) == 1

@pytest.mark.parametrize("operation", ["async_set", "send_command"])
@pytest.mark.parametrize("appliance_type,quiet", [("AC", False), ("AC", True), ("WM", True)])
@pytest.mark.parametrize("status,body,content_type", [
    (503, '{"payload":{"resultCode":"0"}}', "application/json"),
    (200, "{", "application/json"),
    (200, "SYNTHETIC-RESPONSE-SECRET", "text/html"),
    (200, "[]", "application/json"),
    (200, "{}", "application/json"),
    (200, '{"payload":[]}', "application/json"),
    (200, '{"payload":{}}', "application/json"),
    (200, '{"payload":{"resultCode":0}}', "application/json"),
])
async def test_F03_http_protocol_failure_is_safe(server, connection, entry, hass, caplog, operation, appliance_type, quiet, status, body, content_type):
    hass.config_entries.async_update_entry(entry, options={"quiet_ac_commands": quiet})
    requests = []
    async def handler(request):
        requests.append(request.path)
        return web.Response(status=status, text=body, content_type=content_type)
    url = await server(handler)
    caplog.set_level("DEBUG", logger="custom_components.hon")
    with pytest.raises(HomeAssistantError) as caught:
        await send(connection, operation, appliance_type)
    rendered = "".join(traceback.format_exception(caught.value)) + caplog.text
    assert url not in rendered
    assert "SYNTHETIC-RESPONSE-SECRET" not in rendered
    assert len(requests) == 1

async def test_F12_expired_concurrent_operations_share_one_login(server, connection):
    counts = {}
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        counts[request.path] = counts.get(request.path, 0) + 1
        if request.path == "/ciam/authorize":
            entered.set()
            await release.wait()
        if request.path == "/commands/v1/context":
            return web.json_response({"payload": {"shadow": {"parameters": {"tempSel": {"parNewVal": "21"}}}}})
        if request.path == "/commands/v1/send":
            return web.json_response({"payload": {"resultCode": "0"}})
        return auth_reply(request)
    await server(handler)
    connection._start_time = 0
    tasks = [asyncio.create_task(connection.async_get_context(DEVICE)) for _ in range(6)]
    tasks += [asyncio.create_task(send(connection, operation)) for operation in ("async_set", "send_command")]
    await asyncio.wait_for(entered.wait(), 2)
    release.set()
    await asyncio.wait_for(asyncio.gather(*tasks), 3)
    assert counts == {"/ciam/authorize": 1, "/ciam/token": 1, "/unified-api/v1/view/appliance-list": 1, "/commands/v1/context": 6, "/commands/v1/send": 2}

@pytest.mark.parametrize("operation", ["context", "async_set", "send_command"])
@pytest.mark.parametrize("status", [401, 403])
async def test_F13_failed_relogin_blocks_stale_operations(server, connection, operation, status):
    observed = []
    async def handler(request):
        observed.append(request.path)
        return web.json_response({"error": "denied"}, status=status)
    await server(handler)
    connection._start_time = 0
    connection._id_token = "stale-id"
    for _ in range(3):
        with pytest.raises(ConfigEntryAuthFailed):
            if operation == "context":
                await connection.async_get_context(DEVICE)
            else:
                await send(connection, operation)
    assert observed == ["/ciam/authorize"]

async def test_F13_transient_login_failure_cools_down_then_recovers(server, connection, monkeypatch):
    paths = []
    failing = True
    async def handler(request):
        paths.append(request.path)
        if failing:
            return web.json_response({"error": "temporary"}, status=503)
        if request.path == "/commands/v1/context":
            return web.json_response({"payload": {"shadow": {"parameters": {"tempSel": {"parNewVal": "21"}}}}})
        return auth_reply(request)
    await server(handler)
    connection._start_time = 0
    for _ in range(3):
        with pytest.raises(HomeAssistantError) as caught:
            await connection.async_get_context(DEVICE)
        assert not isinstance(caught.value, ConfigEntryAuthFailed)
    assert paths == ["/ciam/authorize"]
    failing = False
    # Advance only the connection cooldown, not the event loop or HA clock.
    connection._login_retry_after = 0
    result = await connection.async_get_context(DEVICE)
    assert result["shadow"]["parameters"]["tempSel"]["parNewVal"] == "21"
    assert paths == ["/ciam/authorize", "/ciam/authorize", "/ciam/token", "/unified-api/v1/view/appliance-list", "/commands/v1/context"]

@pytest.mark.parametrize("operation", ["async_set", "send_command"])
async def test_F13_fresh_session_unauthorized_is_not_replayed(server, connection, operation):
    paths = []
    async def handler(request):
        paths.append(request.path)
        return web.json_response({"error": "denied"}, status=401)
    await server(handler)
    for _ in range(2):
        with pytest.raises(ConfigEntryAuthFailed):
            await send(connection, operation)
    assert paths == ["/commands/v1/send"]

@pytest.mark.parametrize("failure", ["html", "401", "false", "cancel"])
async def test_F14_failed_setup_closes_owned_session(server, hass, entry, failure):
    async def handler(request):
        if failure == "401":
            return web.json_response({"error": "denied"}, status=401)
        return web.Response(text="not json", content_type="text/html")
    await server(handler)
    created = []
    class CapturedConnection(HonConnection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)
        async def async_authorize(self):
            if failure == "false":
                return False
            if failure == "cancel":
                raise asyncio.CancelledError()
            return await super().async_authorize()
    expected = asyncio.CancelledError if failure == "cancel" else (ConfigEntryAuthFailed if failure in ("401", "false") else ConfigEntryNotReady)
    try:
        with patch.object(integration, "HonConnection", CapturedConnection):
            with pytest.raises(expected) as caught:
                await integration.async_setup_entry(hass, entry)
        assert created[0]._session.closed
        assert entry.unique_id not in hass.data.get("hon", {})
        rendered = "".join(traceback.format_exception(caught.value))
        assert "offline-only" not in rendered
        assert "password=" not in rendered
    finally:
        for item in created:
            await item.async_close()

@pytest.mark.parametrize("step", ["user", "reconfigure"])
@pytest.mark.parametrize("failure", ["html", "401", "cancel"])
async def test_F14_config_flow_closes_session_on_every_exit(server, hass, entry, step, failure):
    import custom_components.hon.config_flow as flow_module
    async def handler(request):
        if failure == "401":
            return web.json_response({"error": "denied"}, status=401)
        return web.Response(text="upstream failure", content_type="text/html")
    await server(handler)
    created = []
    class CapturedConnection(HonConnection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)
        async def async_authorize(self):
            if failure == "cancel":
                raise asyncio.CancelledError()
            return await super().async_authorize()
    flow = HonFlowHandler()
    flow.hass = hass
    flow.context = {"entry_id": entry.entry_id, "source": "reconfigure"}
    user_input = {"email": entry.unique_id, "password": "offline-only"}
    try:
        with patch.object(flow_module, "HonConnection", CapturedConnection), patch.object(flow, "async_set_unique_id", AsyncMock()), patch.object(flow, "_abort_if_unique_id_configured"):
            if failure == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await getattr(flow, f"async_step_{step}")(user_input)
            else:
                result = await getattr(flow, f"async_step_{step}")(user_input)
                assert result["type"] == "form"
                assert result["errors"] == {"base": "auth_error"}
        assert created[0]._session.closed
    finally:
        for item in created:
            await item.async_close()
