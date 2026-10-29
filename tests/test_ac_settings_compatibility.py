"""Live-catalog compatibility, with explicitly representative reported snapshots."""
import json
from copy import deepcopy
from pathlib import Path

import pytest

from custom_components.hon.parameter import HonParameterFixed

MODELS = json.loads((Path(__file__).parent / 'fixtures/actual_ac_settings.json').read_text())["models"]


async def install_catalog(device, connection, model):
    """Load the real nested catalog; synthesize a valid OFF basis, not captured telemetry."""
    catalog = {"applianceModel": {}, "options": {}, "dictionaryId": "offline", "settings": deepcopy(model["settings"])}
    async def load(_appliance):
        return deepcopy(catalog)
    connection.load_commands = load
    device.appliance["modelName"] = model["model"]
    await device.load_commands()
    schema = model["settings"]["setParameters"]["parameters"]
    device._attributes = {"parameters": {
        key: str(value.get("defaultValue", value.get("fixedValue", "0")))
        for key, value in schema.items()
    }}
    device.attributes["parameters"].update(onOffStatus="0", machMode="6", tempSel="22.00", tempIndoor="20.00")
    device.coordinator.last_update_success = True


@pytest.mark.parametrize("model", MODELS, ids=["AS25-first", "AS50", "AS25-third"])
@pytest.mark.parametrize("updates", [{"tempSel": 23}, {"tempSel": 23, "onOffStatus": "0"}])
async def test_fixed_power_settings_preserve_off_basis(device, connection, model, updates):
    await install_catalog(device, connection, model)
    before = deepcopy(device.attributes)
    source = device.commands["settings"]
    draft = device.settings_command(updates)
    assert isinstance(draft.parameters["onOffStatus"], HonParameterFixed)
    assert draft.parameters["onOffStatus"].value == "0"
    assert draft.parameters["tempSel"].value == 23
    assert draft.parameters["machMode"].value == "6"
    assert draft.parameters["operationName"].value == "grSetDAC"
    assert source.parameters["onOffStatus"].value == "1"
    assert draft.parameters["onOffStatus"] is not source.parameters["onOffStatus"]
    assert device.attributes == before
    assert model["settings"]["setParameters"]["parameters"]["onOffStatus"]["fixedValue"] == "1"


@pytest.mark.parametrize("power", ["0", "1", 0, 1])
async def test_rebased_power_still_rejects_explicit_conflicts(device, connection, http, power):
    await install_catalog(device, connection, MODELS[0])
    device.attributes["parameters"]["onOffStatus"] = power
    before = deepcopy(device.attributes)
    preserved = str(power)
    assert device.settings_command({"onOffStatus": preserved}).parameters["onOffStatus"].value == preserved
    with pytest.raises(ValueError, match="fixed value"):
        await device.settings_command({"onOffStatus": "1" if preserved == "0" else "0"}).send()
    assert not http.requests
    assert device.attributes == before


@pytest.mark.parametrize("power", [None, "", "bad", "2", 2, True, False, 0.0, "0.0"])
async def test_fixed_power_requires_authoritative_binary_basis(device, connection, http, power):
    await install_catalog(device, connection, MODELS[0])
    device.attributes["parameters"]["onOffStatus"] = power
    with pytest.raises(ValueError, match="reported binary power"):
        await device.settings_command({"onOffStatus": "0", "tempSel": 23}).send()
    assert not http.requests


async def test_fixed_power_cannot_use_basis_after_failed_poll(device, connection, http):
    await install_catalog(device, connection, MODELS[0])
    device.coordinator.last_update_success = False
    with pytest.raises(ValueError, match="reported binary power"):
        await device.settings_command({"onOffStatus": "0", "tempSel": 23}).send()
    assert not http.requests


@pytest.mark.parametrize("field,value", [("operationName", "bad"), ("specialMode", "1"), ("tempSel", 23.5), ("machMode", "3")])
async def test_actual_catalog_other_validation_unchanged(device, connection, http, field, value):
    await install_catalog(device, connection, MODELS[0])
    before = deepcopy(device.attributes)
    with pytest.raises(ValueError):
        await device.settings_command({field: value}).send()
    assert not http.requests
    assert device.attributes == before


@pytest.mark.parametrize("operation,fixed,reported", [("startProgram", "1", "0"), ("stopProgram", "0", "1")])
async def test_intentional_start_stop_power_is_not_rebased(device, connection, operation, fixed, reported):
    from custom_components.hon.command import HonCommand
    await install_catalog(device, connection, MODELS[0])
    schema = deepcopy(MODELS[0]["settings"]["setParameters"])
    schema["parameters"]["onOffStatus"]["fixedValue"] = fixed
    device.commands[operation] = HonCommand(operation, schema, connection, device)
    device.attributes["parameters"]["onOffStatus"] = reported
    draft = device._draft_command(operation, {"onOffStatus": fixed})
    assert draft.parameters["onOffStatus"].value == fixed
    with pytest.raises(ValueError):
        device._draft_command(operation, {"onOffStatus": reported})


async def test_non_ac_fixed_power_is_not_rebased(device, connection):
    await install_catalog(device, connection, MODELS[0])
    device.appliance["applianceTypeName"] = "WM"
    assert device.settings_command().parameters["onOffStatus"].value == "1"
    with pytest.raises(ValueError):
        device.settings_command({"onOffStatus": "0"})


@pytest.mark.parametrize("typology", ["enum", "range"])
async def test_non_fixed_power_retains_explicit_control(device, connection, typology):
    from custom_components.hon.command import HonCommand
    from test_quiet_command_transport import command_schema, enum
    schema = command_schema()
    schema["parameters"]["onOffStatus"] = enum("1", ["0", "1"]) if typology == "enum" else {"typology": "range", "minimumValue": "0", "maximumValue": "1", "incrementValue": "1"}
    device.commands["settings"] = HonCommand("settings", schema, connection, device)
    device.attributes["parameters"]["onOffStatus"] = "0"
    assert str(device.settings_command({"onOffStatus": "1"}).parameters["onOffStatus"].value) == "1"


@pytest.mark.parametrize("fixed", [None, "2", True])
async def test_only_declared_binary_fixed_power_is_rebased(device, connection, fixed):
    from custom_components.hon.command import HonCommand
    await install_catalog(device, connection, MODELS[0])
    schema = deepcopy(MODELS[0]["settings"]["setParameters"])
    schema["parameters"]["onOffStatus"]["fixedValue"] = fixed
    device.commands["settings"] = HonCommand("settings", schema, connection, device)
    device.attributes["parameters"]["onOffStatus"] = "1"
    source = device.commands["settings"].parameters["onOffStatus"]
    assert device.settings_command().parameters["onOffStatus"].value == source.value
    with pytest.raises(ValueError):
        device.settings_command({"onOffStatus": "1"})


@pytest.mark.parametrize("model", MODELS, ids=["AS25-first", "AS50", "AS25-third"])
@pytest.mark.parametrize("power", ["0", "1"])
@pytest.mark.parametrize("quiet", [False, True])
async def test_actual_settings_envelope_preserves_reported_power_on_wire(
    hass, entry, device, connection, http, unchanged_context, model, power, quiet
):
    from test_quiet_command_transport import SEND_URL, sent
    await install_catalog(device, connection, model)
    device.attributes["parameters"]["onOffStatus"] = power
    before = deepcopy(device.attributes)
    hass.config_entries.async_update_entry(entry, options={"quiet_ac_commands": quiet})
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}})
    assert await device.settings_command({"tempSel": 23}).send() is True
    payloads = sent(http)
    assert len(payloads) == 1
    assert payloads[0]["commandName"] == "settings"
    parameters = payloads[0]["parameters"]
    assert set(parameters) == set(model["settings"]["setParameters"]["parameters"])
    assert parameters["operationName"] == "grSetDAC"
    assert parameters["onOffStatus"] == power
    assert parameters["tempSel"] == 23
    assert parameters["machMode"] == "6"
    assert str(parameters["echoStatus"]) == ("1" if quiet else "0")
    assert device.attributes == before  # Unchanged representative GET, not POST echo.
    assert device.coordinator.last_update_success
    assert sum(len(calls) for calls in http.requests.values()) == 5  # One POST, four stale GETs.
