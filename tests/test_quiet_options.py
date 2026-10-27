"""Options use the real HA flow manager and persistent ConfigEntry storage."""

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import SOURCE_RECONFIGURE, ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.loader import async_setup

from custom_components.hon.const import API_URL
from custom_components.hon.hon import HonConnection


async def test_credential_reconfigure_keeps_quiet_preference(
    hass, entry, http, auth_http
):
    hass.config_entries.async_update_entry(entry, options={"quiet_ac_commands": True})
    result = await hass.config_entries.flow.async_init(
        "hon", context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )
    assert result["type"] is FlowResultType.FORM
    assert set(result["data_schema"].schema) == {"password"}
    with patch.object(
        hass.config_entries, "async_reload", new_callable=AsyncMock
    ) as reload:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "replacement-offline-only"}
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reconfigure_successful"
        assert entry.options == {"quiet_ac_commands": True}
        assert entry.data["password"] == "replacement-offline-only"
        assert entry.unique_id == "offline@example.test"
        reload.assert_awaited_once_with(entry.entry_id)


async def test_quiet_option_round_trip_without_reload_or_actuation(
    hass, entry, http, loaded_entry
):
    credentials = dict(entry.data)
    request_count = sum(len(calls) for calls in http.requests.values())
    with patch.object(
        hass.config_entries, "async_reload", new_callable=AsyncMock
    ) as reload:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "init"
        assert result["data_schema"]({}) == {"quiet_ac_commands": False}
        assert len(result["data_schema"].schema) == 1
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"quiet_ac_commands": True}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options == {"quiet_ac_commands": True}
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["data_schema"]({}) == {"quiet_ac_commands": True}
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"quiet_ac_commands": False}
        )
        await hass.async_block_till_done()
        assert entry.options == {"quiet_ac_commands": False}
        reload.assert_not_called()
    assert entry.data == credentials
    assert entry.unique_id == "offline@example.test"
    assert sum(len(calls) for calls in http.requests.values()) == request_count


async def test_option_is_restored_from_disk_in_a_new_ha_instance(hass, entry, http):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"quiet_ac_commands": True}
    )
    # Flush the real HA storage; a new runtime must reconstruct the entry from disk.
    await hass.config_entries._store.async_save(hass.config_entries._data_to_save())
    restarted = HomeAssistant(hass.config.config_dir)
    async_setup(restarted)
    restarted.config_entries = ConfigEntries(restarted, {})
    try:
        await restarted.config_entries.async_initialize()
        restored = restarted.config_entries.async_get_entry(entry.entry_id)
        assert restored is not entry
        assert restored.options == {"quiet_ac_commands": True}
        assert restored.unique_id == entry.unique_id
        assert restored.data == entry.data
        connection = HonConnection(restarted, restored)
        try:
            http.post(
                f"{API_URL}/commands/v1/send", payload={"payload": {"resultCode": "0"}}
            )
            assert await connection.async_set(
                "00:00:00:00:00:01", "AC", {"onOffStatus": "0"}
            )
            http.assert_called_once()
            calls = [call for requests in http.requests.values() for call in requests]
            assert calls[0].kwargs["json"]["parameters"]["echoStatus"] == "1"
        finally:
            await connection.async_close()
    finally:
        await restarted.async_stop(force=True)
