"""Reject malformed contexts before the inherited device mutates its snapshot."""
from copy import deepcopy
import pytest
from aiohttp import web
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

VALID = {"shadow": {"parameters": {"tempSel": {"parNewVal": "23"}}}}

@pytest.mark.parametrize("status,payload", [
    (401, {"error": "denied"}),
    (503, {"payload": VALID}),
    (200, {}),
    (200, {"payload": {}}),
    (200, {"payload": []}),
    (200, {"payload": {"shadow": []}}),
    (200, {"payload": {"shadow": {"parameters": {}}}}),
    (200, {"payload": {"shadow": {"parameters": []}}}),
    (200, {"payload": {"shadow": {"parameters": {"tempSel": "23"}}}}),
    (200, {"payload": {"shadow": {"parameters": {"tempSel": {}}}}}),
    (200, {"payload": {**VALID, "parameters": []}}),
    (200, {"payload": {**VALID, "resultCode": "1"}}),
])
async def test_F07_bad_context_keeps_last_snapshot(server, device, status, payload):
    async def handler(request):
        return web.json_response(payload, status=status)
    await server(handler)
    before = deepcopy(device.attributes)
    expected = ConfigEntryAuthFailed if status == 401 else UpdateFailed
    with pytest.raises(expected):
        await device.load_context()
    assert device.attributes == before
    await device.coordinator.async_refresh()
    assert device.coordinator.last_update_success is False
    assert device.attributes == before

async def test_valid_context_updates_normally(server, device):
    async def handler(request):
        return web.json_response({"payload": VALID})
    await server(handler)
    await device.coordinator.async_refresh()
    assert device.coordinator.last_update_success is True
    assert device.get("tempSel") == "23"
