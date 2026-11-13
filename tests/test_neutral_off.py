"""Real stop catalogs, real HA dispatcher and offline HTTP (not physical proof)."""
import json
from copy import deepcopy
from pathlib import Path

import pytest

from custom_components.hon.command import HonCommand
from test_command_readback import registered_climate, snapshot, CONTEXT_URL
from test_quiet_command_transport import SEND_URL, sent

from homeassistant.exceptions import HomeAssistantError

STOPS = json.loads((Path(__file__).parent / "fixtures/actual_ac_stop_programs.json").read_text())["models"]


@pytest.mark.parametrize("catalog", STOPS, ids=["AS25-first", "AS50", "AS25-third"])
@pytest.mark.parametrize("intent", ["settings", "startProgram"])
@pytest.mark.parametrize("quiet", [False, True])
@pytest.mark.parametrize("service,data", [
    ("turn_off", {}), ("set_hvac_mode", {"hvac_mode": "off"}),
])
async def test_real_stop_supersedes_power_without_replaying_unresolved_parameters(
    hass, entry, device, connection, http, registered_climate, catalog, intent, quiet, service, data
):
    """ACK is not telemetry; OFF preserves unresolved nonpower authorization."""
    hass.config_entries.async_update_entry(entry, options={"quiet_ac_commands": quiet})
    device.commands["stopProgram"] = HonCommand("stopProgram", deepcopy(catalog["stopProgram"]), connection, device)
    source = deepcopy({k: vars(v) for k, v in device.commands["stopProgram"].parameters.items()})
    before = {**device.attributes["parameters"], "onOffStatus": "1", "machMode": "1"}
    device._attributes = {"parameters": deepcopy(before)}
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}}, repeat=True)
    http.get(CONTEXT_URL, status=503)
    command = (device.settings_command({"windSpeed": "3"}) if intent == "settings"
               else device.start_command("iot_cool", {"windSpeed": "3"}))
    assert await command.send() is True
    pending = deepcopy(device._pending_command)
    assert pending["windSpeed"] == "3"
    # Successful but stale GET restores availability, not authorization.
    http.get(CONTEXT_URL, payload=snapshot(before))
    await device.coordinator.async_refresh()
    # The outgoing OFF must not contain stale target/fan/mode/preset fields.
    off = {**before, "onOffStatus": "0"}
    http.get(CONTEXT_URL, payload=snapshot(off))
    await hass.services.async_call("climate", service, {
        "entity_id": registered_climate.entity_id, **data,
    }, blocking=True)
    assert len(sent(http)) == 2
    payload = sent(http)[1]
    assert payload["commandName"] == "startProgram"
    assert payload["programName"] == "PROGRAMS.AC.HOME_ASSISTANT"
    assert payload["attributes"] == {
        "prStr": "HOME_ASSISTANT", "channel": "googleHome", "origin": "conversationalVoice",
    }
    assert payload["parameters"] == {
        "onOffStatus": "0", "machMode": "1", **({"echoStatus": "1"} if quiet else {}),
    }
    assert payload["ancillaryParameters"] == {
        "programFamily": "[standard]", "remoteActionable": "1", "remoteVisible": "1",
    }
    assert device.attributes["parameters"] == off
    assert hass.states.get(registered_climate.entity_id).state == "off"
    assert device._pending_command == {**pending, "onOffStatus": "0"}
    assert await device.settings_command({"tempSel": 24}).send() is False
    assert await device.start_command("iot_cool").send() is False
    assert len(sent(http)) == 2
    assert {k: vars(v) for k, v in device.commands["stopProgram"].parameters.items()} == source


@pytest.mark.parametrize("service,data", [
    ("turn_off", {}), ("set_hvac_mode", {"hvac_mode": "off"}),
])
async def test_native_off_surfaces_withheld_unsupported_stop_schema(
    hass, device, connection, http, registered_climate, service, data
):
    schema = deepcopy(STOPS[0]["stopProgram"])
    schema["parameters"]["operationName"]["fixedValue"] = "unknown-opcode"
    device.commands["stopProgram"] = HonCommand("stopProgram", schema, connection, device)
    before = deepcopy(device.attributes["parameters"])
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}})
    http.get(CONTEXT_URL, status=503)
    assert await device.settings_command({"windSpeed": "3"}).send() is True
    http.get(CONTEXT_URL, payload=snapshot(before))
    await device.coordinator.async_refresh()
    with pytest.raises(HomeAssistantError, match="OFF.*withheld"):
        await hass.services.async_call("climate", service, {
            "entity_id": registered_climate.entity_id, **data,
        }, blocking=True)
    assert len(sent(http)) == 1


@pytest.mark.parametrize("extra", ["rapidMode", "muteStatus", "healthMode", "unknownFeature"])
async def test_fixed_mutable_fields_cannot_disguise_non_neutral_off(
    device, connection, http, registered_climate, extra
):
    before = deepcopy(device.attributes["parameters"])
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}}, repeat=True)
    http.get(CONTEXT_URL, status=503)
    assert await device.settings_command({"windSpeed": "3"}).send() is True
    http.get(CONTEXT_URL, payload=snapshot(before))
    await device.coordinator.async_refresh()
    command = HonCommand("stopProgram", {"parameters": {
        "onOffStatus": {"typology": "fixed", "fixedValue": "0"},
        extra: {"typology": "fixed", "fixedValue": "1"},
    }}, connection, device)
    http.get(CONTEXT_URL, payload=snapshot({**before, "onOffStatus": "0"}), repeat=True)
    assert await command.send() is False
    assert len(sent(http)) == 1


@pytest.mark.parametrize("failure", ["failed", "stale"])
async def test_late_authoritative_off_is_published_without_resolving_old_fields(
    hass, device, connection, http, registered_climate, failure
):
    """Characterization: load_context publishes actual reports, not desired intent."""
    device.commands["stopProgram"] = HonCommand("stopProgram", deepcopy(STOPS[1]["stopProgram"]), connection, device)
    before = {**device.attributes["parameters"], "onOffStatus": "1", "machMode": "1"}
    device._attributes = {"parameters": deepcopy(before)}
    # An OFF built before the preceding SET's acceptance must remain neutral.
    off_command = device.stop_command()
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}}, repeat=True)
    http.get(CONTEXT_URL, status=503)
    assert await device.settings_command({"windSpeed": "3", "rapidMode": "1"}).send() is True
    assert not device.coordinator.last_update_success
    pending = deepcopy(device._pending_command)
    # A new OFF must also build from unavailable/missing reported settings.
    assert set(device.stop_command().parameters) == {"onOffStatus", "operationName"}
    if failure == "failed":
        http.get(CONTEXT_URL, status=503)
    else:
        for _ in range(4):
            http.get(CONTEXT_URL, payload=snapshot(before))
    assert await off_command.send() is True
    assert len(sent(http)) == 2
    assert device.attributes["parameters"] == before  # ACK never invents OFF.
    assert device._pending_command == {**pending, "onOffStatus": "0"}
    off_report = {**before, "onOffStatus": "0", "tempSel": "25.00"}
    http.get(CONTEXT_URL, payload=snapshot(off_report))
    await device.coordinator.async_refresh()
    assert hass.states.get(registered_climate.entity_id).state == "off"
    assert registered_climate.available
    assert device.attributes["parameters"] == off_report
    assert device._pending_command == {**pending, "onOffStatus": "0"}
    assert await device.settings_command({"muteStatus": "1"}).send() is False
    assert len(sent(http)) == 2
    # Only a real report matching all remaining intent can resolve the fence.
    matching = {**off_report, **pending, "onOffStatus": "0"}
    http.get(CONTEXT_URL, payload=snapshot(matching))
    await device.coordinator.async_refresh()
    assert device._pending_command == {}
    assert len(sent(http)) == 2  # No replay of accepted SET or OFF.


@pytest.mark.parametrize("parameters", [{"windSpeed": "5"}, {"tempSel": 22}, {"rapidMode": "0"}])
async def test_explicit_extra_stop_fields_still_use_full_fenced_path(
    device, connection, http, registered_climate, parameters
):
    device.commands["stopProgram"] = HonCommand("stopProgram", deepcopy(STOPS[0]["stopProgram"]), connection, device)
    before = deepcopy(device.attributes["parameters"])
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}})
    http.get(CONTEXT_URL, status=503)
    assert await device.settings_command({"windSpeed": "3"}).send() is True
    http.get(CONTEXT_URL, payload=snapshot(before))
    await device.coordinator.async_refresh()
    assert await device.stop_command({"onOffStatus": "0", **parameters}).send() is False
    assert len(sent(http)) == 1


@pytest.mark.parametrize("invalid", [{"onOffStatus": "1"}, {"onOffStatus": False}, {"badField": "0"}])
async def test_neutral_stop_does_not_skip_explicit_validation(
    device, connection, http, registered_climate, invalid
):
    device.commands["stopProgram"] = HonCommand("stopProgram", deepcopy(STOPS[0]["stopProgram"]), connection, device)
    with pytest.raises(ValueError):
        device.stop_command(invalid)
    assert not http.requests
