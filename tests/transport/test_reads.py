"""All remaining read endpoints share the secret-safe transport boundary."""
import traceback
import pytest
from aiohttp import web
from homeassistant.exceptions import HomeAssistantError, ConfigEntryAuthFailed

async def read(connection, device, operation):
    if operation == "statistics":
        return await connection.load_statistics(device)
    appliance = {**device.appliance, "code": "test", "eepromId": "test", "series": "test"}
    return await connection.load_commands(appliance)

@pytest.mark.parametrize("operation", ["commands", "statistics"])
@pytest.mark.parametrize("status,body,content_type", [
    (401, '{"error":"denied"}', "application/json"),
    (503, '{"payload":{"resultCode":"0"}}', "application/json"),
    (200, "secret response", "text/html"),
    (200, "[]", "application/json"),
    (200, '{"payload":[]}', "application/json"),
])
async def test_F02_read_errors_safe_and_typed(server, connection, device, operation, status, body, content_type, caplog):
    async def handler(request):
        return web.Response(status=status, text=body, content_type=content_type)
    url = await server(handler)
    expected = ConfigEntryAuthFailed if status == 401 else HomeAssistantError
    with pytest.raises(expected) as caught:
        await read(connection, device, operation)
    rendered = "".join(traceback.format_exception(caught.value)) + caplog.text
    assert url not in rendered
    assert "secret response" not in rendered

@pytest.mark.parametrize("operation", ["commands", "statistics"])
async def test_F02_read_debug_never_dumps_response_secrets(server, connection, device, operation, caplog):
    async def handler(request):
        return web.json_response({"payload": {"resultCode": "0", "token": "SYNTHETIC-READ-SECRET"}})
    await server(handler)
    caplog.set_level("DEBUG", logger="custom_components.hon")
    result = await read(connection, device, operation)
    assert result["token"] == "SYNTHETIC-READ-SECRET"
    assert "SYNTHETIC-READ-SECRET" not in caplog.text

@pytest.mark.parametrize("operation", ["commands", "statistics"])
async def test_F13_read_does_not_use_stale_session(server, connection, device, operation):
    paths = []
    async def handler(request):
        paths.append(request.path)
        return web.json_response({"error": "denied"}, status=401)
    await server(handler)
    connection._start_time = 0
    for _ in range(2):
        with pytest.raises(ConfigEntryAuthFailed):
            await read(connection, device, operation)
    assert paths == ["/ciam/authorize"]
