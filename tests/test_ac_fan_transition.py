"""Real captured command schemas; representative telemetry; no real HTTP."""
import json
from copy import deepcopy
from pathlib import Path

import pytest
from aioresponses import CallbackResult
from homeassistant.components import climate as core_climate
from homeassistant.helpers import device_registry, entity_registry

from custom_components.hon.climate import HonClimateEntity
from custom_components.hon.parameter import HonParameterRange
from test_command_readback import CONTEXT_URL, snapshot
from test_quiet_command_transport import SEND_URL, sent

MODELS = json.loads((Path(__file__).parent / "fixtures/actual_ac_fan_programs.json").read_text())["models"]
MODEL_IDS = ["AS25-first", "AS50", "AS25-third"]


async def install_fan_catalog(hass, entry, device, connection, model):
    """Load real schemas normally; seed a representative HEAT/AUTO-speed basis."""
    catalog = {"applianceModel": {}, "options": {}, "dictionaryId": "offline",
               "settings": deepcopy(model["settings"]), "startProgram": deepcopy(model["startProgram"])}

    async def load(_appliance):
        return deepcopy(catalog)

    connection.load_commands = load
    device.appliance["modelName"] = model["model"]
    await device.load_commands()
    reported = {}
    for schema in [model["settings"]["setParameters"], *model["startProgram"].values()]:
        for key, value in schema["parameters"].items():
            reported[key] = str(value.get("defaultValue", value.get("fixedValue", "0")))
    reported.update(onOffStatus="1", machMode="4", windSpeed="5", tempSel="22.00", tempIndoor="20.00")
    device._attributes = {"parameters": reported}
    device.coordinator.last_update_success = True
    hass.data["hon"] = {entry.unique_id: connection}
    return HonClimateEntity(hass, device.coordinator, entry, device.appliance)


def schema_state(device):
    """Include every source draft's identity and parameter state, including SETTINGS."""
    commands = list(device.commands.values())
    commands += list(device.commands["startProgram"].get_programs().values())
    return [(id(command), {key: (id(p), p.value if key == "program" else deepcopy(vars(p)))
                           for key, p in command.parameters.items()}) for command in commands]


@pytest.fixture
async def fan_climate(hass, entry, device, connection, request):
    climate = await install_fan_catalog(hass, entry, device, connection, request.param)
    device_registry.async_setup(hass)
    await device_registry.async_load(hass)
    await entity_registry.async_load(hass)
    assert await core_climate.async_setup(hass, {})
    component = hass.data[core_climate.DATA_COMPONENT]
    component._platforms["climate"].config_entry = entry
    await component.async_add_entities([climate])
    yield climate
    await climate.async_remove()


@pytest.mark.parametrize("fan_climate", MODELS, ids=MODEL_IDS, indirect=True)
@pytest.mark.parametrize("combined", [False, True], ids=["mode", "mode-target"])
@pytest.mark.parametrize("quiet", [False, True])
async def test_implicit_auto_to_fan_is_one_start_with_program_default(
    hass, entry, device, connection, http, fan_climate, combined, quiet
):
    climate = fan_climate
    hass.config_entries.async_update_entry(entry, options={"quiet_ac_commands": quiet})
    before = deepcopy(device.attributes)
    sources = schema_state(device)
    # AUTO is valid in SETTINGS, but not in the selected FAN program.
    assert "5" in device.commands["settings"].parameters["windSpeed"].values
    fan = device.commands["startProgram"].get_programs()["iot_fan"]
    assert fan.parameters["windSpeed"].values == ["1", "2", "3"]
    assert fan.parameters["windSpeed"].default == "3"
    assert device.settings_command().parameters["windSpeed"].value == "5"
    events = []

    def unchanged():
        assert device.attributes == before
        assert schema_state(device) == sources
        assert climate.hvac_mode == "heat"
        assert climate.fan_mode == "auto"
        assert climate.target_temperature == 22

    def post(url, **kwargs):
        unchanged()
        events.append("POST")
        return CallbackResult(payload={"payload": {"resultCode": "0"}})

    def get(url, **kwargs):
        unchanged()
        assert not connection._command_locks[device.mac_address].locked()
        events.append("GET")
        return CallbackResult(payload=snapshot(before["parameters"]))

    http.post(SEND_URL, callback=post)
    http.get(CONTEXT_URL, callback=get)
    data = {"entity_id": climate.entity_id, "hvac_mode": "fan_only"}
    if combined:
        data["temperature"] = 23
    await hass.services.async_call("climate", "set_temperature" if combined else "set_hvac_mode", data, blocking=True)
    payloads = sent(http)
    assert len(payloads) == 1
    assert payloads[0]["commandName"] == "startProgram"
    wire = payloads[0]["parameters"]
    expected = {}
    for key, p in fan.parameters.items():
        if key == "program":
            continue
        expected[key] = (float(before["parameters"][key]) if isinstance(p, HonParameterRange)
                         else before["parameters"][key]) if p.typology != "fixed" else p.value
    expected.update(windSpeed="3", machMode="6", onOffStatus="1", tempSel=23 if combined else 22)
    if quiet:
        expected["echoStatus"] = "1"  # Existing quiet policy overrides the numeric range draft.
    assert wire == expected
    assert events == ["POST", "GET"]
    unchanged()  # A stale readback must not be replaced by an optimistic command echo.


@pytest.mark.parametrize("model", MODELS, ids=MODEL_IDS)
@pytest.mark.parametrize("values", ["123", "1|2|3", {"1": {}, "2": {}, "3": {}}])
async def test_malformed_fan_enum_container_rejected_before_substitution(
    hass, entry, device, connection, http, model, values
):
    changed = deepcopy(model)
    changed["startProgram"]["PROGRAMS.AC.IOT_FAN"]["parameters"]["windSpeed"]["enumValues"] = values
    climate = await install_fan_catalog(hass, entry, device, connection, changed)
    before, sources = deepcopy(device.attributes), schema_state(device)
    with pytest.raises(ValueError):
        await climate.async_set_temperature(hvac_mode="fan_only", temperature=23)
    assert not http.requests
    assert device.attributes == before
    assert schema_state(device) == sources


@pytest.mark.parametrize("model", MODELS, ids=MODEL_IDS)
@pytest.mark.parametrize("default", [None, "", "5", "4", 3, True])
async def test_missing_or_invalid_fan_default_fails_closed(hass, entry, device, connection, http, model, default):
    changed = deepcopy(model)
    speed = changed["startProgram"]["PROGRAMS.AC.IOT_FAN"]["parameters"]["windSpeed"]
    if default is None:
        speed.pop("defaultValue")
    else:
        speed["defaultValue"] = default
    climate = await install_fan_catalog(hass, entry, device, connection, changed)
    before, sources = deepcopy(device.attributes), schema_state(device)
    with pytest.raises(ValueError):
        await climate.async_set_temperature(hvac_mode="fan_only", temperature=23)
    assert not http.requests
    assert device.attributes == before
    assert schema_state(device) == sources


@pytest.mark.parametrize("model", MODELS, ids=MODEL_IDS)
async def test_transition_uses_declared_default_not_mutable_schema_value(hass, entry, device, connection, model):
    changed = deepcopy(model)
    changed["startProgram"]["PROGRAMS.AC.IOT_FAN"]["parameters"]["windSpeed"]["defaultValue"] = "2"
    climate = await install_fan_catalog(hass, entry, device, connection, changed)
    fan_speed = device.commands["startProgram"].get_programs()["iot_fan"].parameters["windSpeed"]
    fan_speed.value = "1"
    before, sources = deepcopy(device.attributes), schema_state(device)
    intent = {"tempSel": 23}
    draft = climate._mode_command("fan_only", intent)
    assert draft.parameters["windSpeed"].value == "2"
    assert intent == {"tempSel": 23}
    assert device.attributes == before
    assert schema_state(device) == sources


@pytest.mark.parametrize("model", MODELS, ids=MODEL_IDS)
@pytest.mark.parametrize("speed", ["1", "2", "3"])
async def test_supported_reported_speed_is_preserved(hass, entry, device, connection, model, speed):
    climate = await install_fan_catalog(hass, entry, device, connection, model)
    device.attributes["parameters"]["windSpeed"] = speed
    sources = schema_state(device)
    assert climate._mode_command("fan_only").parameters["windSpeed"].value == speed
    assert schema_state(device) == sources


@pytest.mark.parametrize("model", MODELS, ids=MODEL_IDS)
async def test_program_supporting_auto_does_not_substitute_default(hass, entry, device, connection, model):
    changed = deepcopy(model)
    changed["startProgram"]["PROGRAMS.AC.IOT_FAN"]["parameters"]["windSpeed"]["enumValues"].append(5)
    climate = await install_fan_catalog(hass, entry, device, connection, changed)
    assert climate._mode_command("fan_only").parameters["windSpeed"].value == "5"


@pytest.mark.parametrize("model", MODELS, ids=MODEL_IDS)
@pytest.mark.parametrize("reported", [None, "bad", "4", 5])
async def test_only_reported_string_auto_gets_transition_policy(hass, entry, device, connection, http, model, reported):
    climate = await install_fan_catalog(hass, entry, device, connection, model)
    device.attributes["parameters"]["windSpeed"] = reported
    with pytest.raises(ValueError):
        await climate.async_set_hvac_mode("fan_only")
    assert not http.requests


@pytest.mark.parametrize("model", MODELS, ids=MODEL_IDS)
@pytest.mark.parametrize("field,value", [("tempSel", None), ("tempSel", "bad"), ("echoStatus", "bad")])
async def test_other_invalid_reported_fields_are_not_defaulted(hass, entry, device, connection, http, model, field, value):
    climate = await install_fan_catalog(hass, entry, device, connection, model)
    device.attributes["parameters"][field] = value
    before, sources = deepcopy(device.attributes), schema_state(device)
    with pytest.raises(ValueError):
        await climate.async_set_hvac_mode("fan_only")
    assert not http.requests
    assert device.attributes == before
    assert schema_state(device) == sources


@pytest.mark.parametrize("missing", ["program", "windSpeed", "enum"])
async def test_missing_fan_schema_fails_closed(hass, entry, device, connection, http, missing):
    changed = deepcopy(MODELS[0])
    fan = changed["startProgram"]["PROGRAMS.AC.IOT_FAN"]
    if missing == "program":
        changed["startProgram"].pop("PROGRAMS.AC.IOT_FAN")
    elif missing == "windSpeed":
        fan["parameters"].pop("windSpeed")
    else:
        fan["parameters"]["windSpeed"] = {"typology": "fixed", "fixedValue": "3"}
    climate = await install_fan_catalog(hass, entry, device, connection, changed)
    with pytest.raises(ValueError):
        await climate.async_set_hvac_mode("fan_only")
    assert not http.requests


@pytest.mark.parametrize("model", MODELS, ids=MODEL_IDS)
@pytest.mark.parametrize("invalid", ["5", "bad", None, 3])
async def test_explicit_invalid_raw_start_program_rejected_before_post(
    hass, entry, loaded_entry, device, connection, http, model, invalid
):
    # loaded_entry owns a separate connection; close it before replacing its key.
    await hass.data["hon"][entry.unique_id].async_close()
    climate = await install_fan_catalog(hass, entry, device, connection, model)
    device_registry.async_setup(hass)
    await device_registry.async_load(hass)
    await entity_registry.async_load(hass)
    target = device_registry.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("hon", device.mac_address, "AC")})
    connection._coordinator_dict[device.mac_address] = device.coordinator
    before, sources = deepcopy(device.attributes), schema_state(device)
    # Explicit invalid intent must be rejected by the adapter too, never replaced.
    with pytest.raises(ValueError):
        climate._mode_command("fan_only", {"windSpeed": invalid})
    from homeassistant.exceptions import HomeAssistantError
    with pytest.raises((ValueError, HomeAssistantError)):
        await hass.services.async_call("hon", "start_program", {
            "device_id": [target.id], "program": "iot_fan",
            "parameters": {"windSpeed": invalid, "machMode": "6", "onOffStatus": "1"}
        }, blocking=True)
    assert all(str(url) != SEND_URL for method, url in http.requests)
    assert device.attributes == before
    assert schema_state(device) == sources


@pytest.mark.parametrize("fan_climate", MODELS, ids=MODEL_IDS, indirect=True)
@pytest.mark.parametrize("restore_mode", ["heat", "auto"])
@pytest.mark.parametrize("combined", [False, True])
async def test_heat_auto_fan_roundtrip_uses_actual_readback(
    hass, device, connection, http, fan_climate, restore_mode, combined
):
    climate = fan_climate
    # Start with representative HEAT or AUTO, both with AUTO speed.
    device.attributes["parameters"]["machMode"] = "4" if restore_mode == "heat" else "0"
    climate._handle_coordinator_update()
    sources = schema_state(device)
    remote = deepcopy(device.attributes["parameters"])
    prior = deepcopy(remote)
    events = []

    def post(url, **kwargs):
        nonlocal remote, prior
        assert device.attributes["parameters"] == remote
        assert schema_state(device) == sources
        prior = deepcopy(remote)
        wire = kwargs["json"]
        remote.update({key: str(value) for key, value in wire["parameters"].items()})
        events.append(("POST", wire["commandName"]))
        return CallbackResult(payload={"payload": {"resultCode": "0"}})

    def get(url, **kwargs):
        assert not connection._command_locks[device.mac_address].locked()
        assert device.attributes["parameters"] == prior
        assert schema_state(device) == sources
        events.append(("GET", remote["windSpeed"]))
        return CallbackResult(payload=snapshot(remote))

    http.post(SEND_URL, callback=post, repeat=True)
    http.get(CONTEXT_URL, callback=get, repeat=True)
    for service, intent, expected_mode, expected_speed in [
        ("set_temperature" if combined else "set_hvac_mode",
         {"hvac_mode": "fan_only", **({"temperature": 23} if combined else {})}, "fan_only", "low"),
        ("set_hvac_mode", {"hvac_mode": restore_mode}, restore_mode, "low"),
        ("set_fan_mode", {"fan_mode": "auto"}, restore_mode, "auto"),
        ("set_hvac_mode", {"hvac_mode": "fan_only"}, "fan_only", "low"),
    ]:
        await hass.services.async_call("climate", service, {"entity_id": climate.entity_id, **intent}, blocking=True)
        assert device.attributes["parameters"] == remote
        assert hass.states.get(climate.entity_id).state == expected_mode
        assert climate.fan_mode == expected_speed
        assert climate.target_temperature == (23 if combined else 22)
        assert device.coordinator.last_update_success
    assert events == [("POST", "startProgram"), ("GET", "3"), ("POST", "startProgram"),
                      ("GET", "3"), ("POST", "settings"), ("GET", "5"),
                      ("POST", "startProgram"), ("GET", "3")]
    assert len(sent(http)) == 4
    assert schema_state(device) == sources
