"""Actual HA coordinator + HTTP readback; all server snapshots are representative."""
import asyncio
import re
from copy import deepcopy

import pytest
from aioresponses import CallbackResult
from homeassistant.components import climate as core_climate
from homeassistant.helpers import device_registry, entity_registry

from custom_components.hon.climate import HonClimateEntity
from custom_components.hon.command import HonCommand
from custom_components.hon.const import API_URL
from test_ac_settings_compatibility import MODELS, install_catalog
from test_quiet_command_transport import SEND_URL, sent

CONTEXT_URL = re.compile(re.escape(f"{API_URL}/commands/v1/context") + r"\?.*")


def snapshot(parameters):
    """Representative remote shadow, never a fabricated production capture."""
    return {"payload": {"shadow": {"parameters": {
        key: {"parNewVal": value} for key, value in parameters.items()
    }}}}


@pytest.fixture
async def registered_climate(hass, entry, device, connection):
    await install_catalog(device, connection, MODELS[1])
    # Representative intentional start schema, derived from settings for this test.
    schema = deepcopy(MODELS[1]["settings"]["setParameters"])
    schema["parameters"]["machMode"] = {"typology": "fixed", "fixedValue": "1"}
    start = HonCommand("startProgram", schema, connection, device)
    start._multi = {"iot_cool": start}
    device.commands["startProgram"] = start
    hass.data["hon"] = {entry.unique_id: connection}
    climate = HonClimateEntity(hass, device.coordinator, entry, device.appliance)
    device_registry.async_setup(hass)
    await device_registry.async_load(hass)
    await entity_registry.async_load(hass)
    assert await core_climate.async_setup(hass, {})
    component = hass.data[core_climate.DATA_COMPONENT]
    component._platforms["climate"].config_entry = entry
    await component.async_add_entities([climate])
    yield climate
    await climate.async_remove()


async def test_consecutive_mode_target_get_actual_readback_before_return(
    hass, device, connection, http, registered_climate
):
    climate = registered_climate
    events = []
    initial = deepcopy(device.attributes["parameters"])
    mode_report = {**initial, "onOffStatus": "1", "machMode": "1"}
    target_report = {**mode_report, "tempSel": "23.00"}

    def post(url, **kwargs):
        events.append(("POST", kwargs["json"]["commandName"]))
        return CallbackResult(payload={"payload": {"resultCode": "0"}})

    def readback(expected_old, reported):
        def get(url, **kwargs):
            # GET happens after the POST lock is released, with no optimistic write.
            assert not connection._command_locks[device.mac_address].locked()
            assert device.attributes["parameters"] == expected_old
            events.append(("GET", reported["tempSel"]))
            return CallbackResult(payload=snapshot(reported))
        return get

    http.post(SEND_URL, callback=post, repeat=True)
    http.get(CONTEXT_URL, callback=readback(initial, mode_report))
    http.get(CONTEXT_URL, callback=readback(mode_report, target_report))
    async with asyncio.timeout(1):
        await hass.services.async_call("climate", "set_hvac_mode", {
            "entity_id": climate.entity_id, "hvac_mode": "cool"
        }, blocking=True)
        state = hass.states.get(climate.entity_id)
        assert state.state == "cool"
        assert state.attributes["temperature"] == 22
        assert device.attributes["parameters"] == mode_report
        # Deliberately consecutive, no clock advance: request_refresh would debounce.
        await hass.services.async_call("climate", "set_temperature", {
            "entity_id": climate.entity_id, "temperature": 23
        }, blocking=True)
        state = hass.states.get(climate.entity_id)
        assert state.state == "cool"
        assert state.attributes["temperature"] == 23
        assert device.attributes["parameters"] == target_report
    assert device.coordinator.last_update_success
    assert events == [("POST", "startProgram"), ("GET", "22.00"), ("POST", "settings"), ("GET", "23.00")]
    payloads = sent(http)
    assert len(payloads) == 2
    assert payloads[1]["parameters"]["machMode"] == "1"
    assert payloads[1]["parameters"]["onOffStatus"] == "1"


@pytest.mark.parametrize("failure", ["http", "invalid", "auth"])
async def test_accepted_post_failed_get_is_unavailable_without_replay(
    hass, device, http, registered_climate, failure
):
    climate = registered_climate
    before = deepcopy(device.attributes)
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}})
    if failure == "invalid":
        http.get(CONTEXT_URL, payload={"payload": {"shadow": {"parameters": {}}}})
    else:
        http.get(CONTEXT_URL, status=401 if failure == "auth" else 503)
    async with asyncio.timeout(1):
        # True remains cloud acceptance only. The coordinator records GET failure.
        assert await device.settings_command({"tempSel": 23}).send() is True
    assert not device.coordinator.last_update_success
    assert not climate.available
    assert hass.states.get(climate.entity_id).state == "unavailable"
    assert device.attributes == before
    assert len(sent(http)) == 1
    assert sum(len(calls) for calls in http.requests.values()) == 2
    # A subsequent ordinary draft cannot silently reuse this failed basis.
    with pytest.raises(ValueError):
        device.settings_command({"tempSel": 24})
    assert len(sent(http)) == 1


async def test_accepted_post_stale_get_does_not_invent_physical_success(
    hass, device, http, registered_climate
):
    climate = registered_climate
    before = deepcopy(device.attributes)
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}})
    http.get(CONTEXT_URL, payload=snapshot(before["parameters"]))
    await hass.services.async_call("climate", "set_temperature", {
        "entity_id": climate.entity_id, "temperature": 23
    }, blocking=True)
    assert device.coordinator.last_update_success
    assert hass.states.get(climate.entity_id).state == "off"
    assert hass.states.get(climate.entity_id).attributes["temperature"] == 22
    assert device.attributes == before
    assert len(sent(http)) == 1


async def test_rejected_post_has_no_followup_get(hass, device, http, registered_climate):
    from homeassistant.exceptions import HomeAssistantError
    before = deepcopy(device.attributes)
    http.post(SEND_URL, payload={"payload": {"resultCode": "1"}})
    with pytest.raises(HomeAssistantError):
        await device.settings_command({"tempSel": 23}).send()
    assert device.attributes == before
    assert device.coordinator.last_update_success
    assert sum(len(calls) for calls in http.requests.values()) == 1
    assert len(sent(http)) == 1
