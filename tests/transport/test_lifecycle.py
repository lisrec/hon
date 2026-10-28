"""Failed setup cleanup and native HA reauthentication flow."""
import asyncio
from unittest.mock import AsyncMock, patch
import pytest
from aiohttp import web
from homeassistant.exceptions import ConfigEntryNotReady
import custom_components.hon as integration
from custom_components.hon.hon import HonConnection

APPLIANCE = {
    "macAddress": "00:00:00:00:00:01", "applianceTypeName": "AC", "applianceTypeId": 11,
    "brand": "Offline", "modelName": "Synthetic AC", "fwVersion": "test",
    "applianceModelId": "test", "serialNumber": "test", "connectivity": "wifi",
    "code": "test", "eepromId": "test", "series": "test",
}

async def auth_handler(request):
    if request.path == "/ciam/authorize":
        return web.json_response({"session_id": "synthetic-session"})
    if request.path == "/ciam/token":
        return web.json_response({"tokens": {"id_token": "synthetic-id", "cognito_token": "synthetic-cognito"}})
    if request.path == "/unified-api/v1/view/appliance-list":
        return web.json_response({"modules": {"applianceList": {"payload": {"appliances": [APPLIANCE]}}}})
    if request.path == "/commands/v1/context":
        return web.json_response({"payload": {"shadow": {"parameters": {"tempSel": {"parNewVal": "21"}}}}})
    if request.path == "/commands/v1/retrieve":
        return web.json_response({"payload": {"resultCode": "0", "applianceModel": {}, "options": {}, "dictionaryId": "test"}})
    if request.path == "/commands/v1/statistics":
        return web.json_response({"payload": {}})
    raise AssertionError(request.path)

@pytest.mark.parametrize("cancel", [False, True])
async def test_F14_late_setup_failure_shuts_down_coordinators(server, hass, entry, cancel):
    await server(auth_handler)
    created = []
    class CapturedConnection(HonConnection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)
    failure = asyncio.CancelledError() if cancel else RuntimeError("synthetic forwarding failure")
    from homeassistant.config_entries import current_entry, ConfigEntryState
    entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
    token = current_entry.set(entry)
    forward = AsyncMock(side_effect=failure)
    try:
        with patch.object(integration, "HonConnection", CapturedConnection), patch.object(hass.config_entries, "async_forward_entry_setups", forward):
            with pytest.raises(asyncio.CancelledError if cancel else ConfigEntryNotReady):
                await integration.async_setup_entry(hass, entry)
        forward.assert_awaited_once()
        assert created[0]._session.closed
        assert entry.unique_id not in hass.data.get("hon", {})
        assert created[0]._coordinator_dict
        assert all(item._shutdown_requested for item in created[0]._coordinator_dict.values())
    finally:
        current_entry.reset(token)
        for item in created:
            await item.async_close()

async def test_F13_native_reauth_updates_existing_entry_and_keeps_options(server, hass, entry):
    async def handler(request):
        if request.path == "/unified-api/v1/view/appliance-list":
            return web.json_response({"modules": {"applianceList": {"payload": {"appliances": []}}}})
        return await auth_handler(request)
    await server(handler)
    hass.config_entries.async_update_entry(entry, options={"quiet_ac_commands": True})
    result = await hass.config_entries.flow.async_init(
        "hon", context={"source": "reauth", "entry_id": entry.entry_id, "unique_id": entry.unique_id}, data=entry.data
    )
    assert result["type"] == "form"
    assert set(result["data_schema"].schema) == {"password"}
    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "replacement-offline-only"})
        await hass.async_block_till_done()
    assert result["type"] == "abort"
    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == "replacement-offline-only"
    assert entry.options == {"quiet_ac_commands": True}
    assert entry.unique_id == "offline@example.test"
    reload.assert_awaited_once_with(entry.entry_id)

async def test_F02_setup_does_not_log_auth_inventory_body(server, hass, entry, caplog):
    from homeassistant.config_entries import current_entry, ConfigEntryState
    async def handler(request):
        if request.path == "/unified-api/v1/view/appliance-list":
            return web.json_response({"modules": {"applianceList": {"payload": {"appliances": [{**APPLIANCE, "id_token": "SYNTHETIC-INVENTORY-SECRET"}]}}}})
        return await auth_handler(request)
    await server(handler)
    caplog.set_level("DEBUG", logger="custom_components.hon")
    entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
    token = current_entry.set(entry)
    try:
        with patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()):
            assert await integration.async_setup_entry(hass, entry)
        assert "SYNTHETIC-INVENTORY-SECRET" not in caplog.text
    finally:
        current_entry.reset(token)
        hon = hass.data.get("hon", {}).get(entry.unique_id)
        if hon is not None:
            await hon.async_close()
