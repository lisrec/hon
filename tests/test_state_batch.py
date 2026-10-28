"""Desired offline behavior; schemas are representative, never hardware captures."""

from copy import deepcopy
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.hon.command import HonCommand
from custom_components.hon.parameter import HonParameterRange
from test_quiet_command_transport import command_schema, enum, sent, SEND_URL

pytestmark = pytest.mark.usefixtures("unchanged_context")


def install_settings(device, connection):
    device.commands["settings"] = HonCommand(
        "settings", command_schema(), connection, device
    )


async def test_draft_does_not_mutate_reported_or_schema_state(device, connection):
    install_settings(device, connection)
    before = deepcopy(device.attributes)
    command = device.settings_command({"tempSel": 23})
    assert device.attributes == before
    assert device.commands["settings"].parameters["tempSel"].value == 21
    assert command.parameters["tempSel"].value == 23


@pytest.mark.parametrize(
    "updates",
    [
        {"tempSel": 99},
        {"unknown": 1},
        {"windSpeed": "bad"},
        {"tempSel": 23, "windSpeed": "bad"},
    ],
)
async def test_invalid_intent_rejected_without_mutation(
    device, connection, http, updates
):
    install_settings(device, connection)
    before = deepcopy(device.attributes)
    with pytest.raises(ValueError):
        await device.settings_command(updates).send()
    assert not http.requests
    assert device.attributes == before
    assert device.commands["settings"].parameters["tempSel"].value == 21


@pytest.mark.parametrize("operation", ["startProgram", "stopProgram"])
async def test_all_command_drafts_are_isolated(device, connection, operation):
    device.commands[operation] = HonCommand(
        operation, command_schema(), connection, device
    )
    before = deepcopy(device.attributes)
    builder = (
        device.start_command if operation == "startProgram" else device.stop_command
    )
    with pytest.raises(ValueError):
        builder(parameters={"tempSel": 23, "windSpeed": "bad"})
    assert device.commands[operation].parameters["tempSel"].value == 21
    assert device.attributes == before
    draft = builder(parameters={"tempSel": 23})
    assert draft is not device.commands[operation]
    assert device.attributes == before


@pytest.mark.parametrize("invert", [False, True])
@pytest.mark.parametrize("turn_on", [False, True])
async def test_switch_uses_synced_draft_without_optimistic_telemetry(
    hass, entry, device, connection, http, invert, turn_on
):
    from custom_components.hon.switch import HonSwitchEntity, HonSwitchEntityDescription

    schema = command_schema()
    schema["parameters"]["screenDisplayStatus"] = enum("0", ["0", "1"])
    device.commands["settings"] = HonCommand("settings", schema, connection, device)
    device.attributes["parameters"].update(
        tempSel="26", windSpeed="5", screenDisplayStatus="0"
    )
    switch = HonSwitchEntity(
        hass,
        device.coordinator,
        entry,
        device.appliance,
        HonSwitchEntityDescription(key="screenDisplayStatus", name="Screen"),
        invert=invert,
    )
    before = deepcopy(device.attributes)
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}})
    await (switch.async_turn_on() if turn_on else switch.async_turn_off())
    payload = sent(http)[0]["parameters"]
    assert payload["tempSel"] == 26
    assert payload["windSpeed"] == "5"
    assert payload["screenDisplayStatus"] == ("1" if turn_on != invert else "0")
    assert device.attributes == before


@pytest.mark.parametrize(
    "context", [None, {}, {"shadow": {}}, {"shadow": {"parameters": {"tempSel": {}}}}]
)
async def test_invalid_context_keeps_last_snapshot(device, context):
    from homeassistant.helpers.update_coordinator import UpdateFailed

    device._hon.async_get_context = AsyncMock(return_value=context)
    before = deepcopy(device.attributes)
    with pytest.raises(UpdateFailed):
        await device.load_context()
    assert device.attributes == before


def make_climate(hass, entry, device, connection):
    from custom_components.hon.climate import HonClimateEntity

    hass.data["hon"] = {entry.unique_id: connection}
    install_settings(device, connection)
    return HonClimateEntity(hass, device.coordinator, entry, device.appliance)


async def test_climate_poll_failure_is_unavailable(hass, entry, device, connection):
    climate = make_climate(hass, entry, device, connection)
    device.coordinator.last_update_success = True
    assert climate.available
    device._hon.async_get_context = AsyncMock(side_effect=HomeAssistantError("offline"))
    await device.coordinator.async_refresh()
    assert not device.coordinator.last_update_success
    assert not climate.available


async def test_combined_climate_dispatch_sends_mode_and_target_once(
    hass, entry, device, connection, http
):
    from homeassistant.components import climate as core_climate
    from homeassistant.components.climate.const import HVACMode

    climate = make_climate(hass, entry, device, connection)
    multi = {}
    for name, mode in [("iot_cool", "1"), ("iot_heat", "4")]:
        schema = command_schema()
        schema["parameters"]["machMode"] = {"typology": "fixed", "fixedValue": mode}
        schema["parameters"]["onOffStatus"] = {"typology": "fixed", "fixedValue": "1"}
        multi[name] = HonCommand(
            "startProgram", schema, connection, device, multi=multi, program=name
        )
    for command in multi.values():
        command._multi = multi
    device.commands["startProgram"] = multi["iot_heat"]
    from homeassistant.helpers import entity_registry, device_registry

    device_registry.async_setup(hass)
    await device_registry.async_load(hass)
    await entity_registry.async_load(hass)
    assert await core_climate.async_setup(hass, {})
    component = hass.data[core_climate.DATA_COMPONENT]
    device.coordinator.last_update_success = True
    await component.async_add_entities([climate])
    before = deepcopy(device.attributes)
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}})
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {"entity_id": climate.entity_id, "hvac_mode": "cool", "temperature": 23},
        blocking=True,
    )
    payload = sent(http)
    assert len(payload) == 1
    assert payload[0]["commandName"] == "startProgram"
    assert payload[0]["parameters"]["machMode"] == "1"
    assert payload[0]["parameters"]["tempSel"] == 23
    assert climate.hvac_mode == HVACMode.HEAT
    assert climate.target_temperature == 21
    assert device.attributes == before
    await climate.async_remove()


async def test_number_setup_creates_each_command_parameter_once(
    hass, entry, device, connection
):
    from custom_components.hon import number

    hass.data["hon"] = {entry.unique_id: connection}
    connection._appliances = [device.appliance]
    connection.async_get_coordinator = AsyncMock(return_value=device.coordinator)
    device.commands["startProgram"] = HonCommand(
        "startProgram",
        {"parameters": {"delayTime": rng("0", "24", "1", "0")}},
        connection,
        device,
    )
    added = Mock()
    with patch.object(
        number.translation, "async_get_translations", AsyncMock(return_value={})
    ):
        await number.async_setup_entry(hass, entry, added)
    entities = added.call_args.args[0]
    assert len(entities) == 1
    assert len({entity.unique_id for entity in entities}) == len(entities)


async def test_raw_service_routes_to_active_device_owner_after_first_unload(
    hass, entry, loaded_entry, device, connection, http
):
    from custom_components.hon import async_unload_entry
    from homeassistant.helpers import device_registry, entity_registry

    device_registry.async_setup(hass)
    await device_registry.async_load(hass)
    await entity_registry.async_load(hass)
    reg = device_registry.async_get(hass)
    target = reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("hon", device.mac_address, "AC")}
    )
    connection._coordinator_dict[device.mac_address] = device.coordinator
    hass.data["hon"]["second-account"] = connection
    install_settings(device, connection)
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}}, repeat=True)
    for unload_first in [False, True]:
        if unload_first:
            with patch.object(
                hass.config_entries,
                "async_unload_platforms",
                AsyncMock(return_value=True),
            ):
                assert await async_unload_entry(hass, entry)
        await hass.services.async_call(
            "hon",
            "update_settings",
            {"device_id": [target.id], "parameters": {"tempSel": 23}},
            blocking=True,
        )
    assert len(sent(http)) == 2
    assert all(payload["parameters"]["tempSel"] == 23 for payload in sent(http))
    # loaded_entry's fixture owns this key during teardown.
    hass.data["hon"][entry.unique_id] = connection


async def test_legacy_service_resolves_owner_not_setup_account(
    hass, entry, loaded_entry, device, connection, http
):
    from homeassistant.helpers import device_registry

    device_registry.async_setup(hass)
    await device_registry.async_load(hass)
    target = device_registry.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("hon", device.mac_address, "AC")}
    )
    connection._coordinator_dict[device.mac_address] = device.coordinator
    hass.data["hon"]["second-account"] = connection
    connection._cognitoToken = "second-account-offline"
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}})
    await hass.services.async_call(
        "hon", "turn_on_purifier", {"device": target.id}, blocking=True
    )
    assert len(sent(http)) == 1
    request = next(
        calls for (method, url), calls in http.requests.items() if str(url) == SEND_URL
    )[0]
    assert request.kwargs["headers"]["cognito-token"] == "second-account-offline"


@pytest.mark.parametrize(
    "method,args,attribute",
    [
        ("async_turn_off", {}, "hvac_mode"),
        ("async_set_hvac_mode", {"hvac_mode": "off"}, "hvac_mode"),
        ("async_set_fan_mode", {"fan_mode": "high"}, "fan_mode"),
        ("async_set_screen_display", {"screen_display": True}, "_screen_display"),
    ],
)
@pytest.mark.parametrize("accepted", [False, True])
async def test_ack_is_not_reported_climate_telemetry(
    hass, entry, device, connection, http, method, args, attribute, accepted
):
    climate = make_climate(hass, entry, device, connection)
    schema = command_schema()
    schema["parameters"]["screenDisplayStatus"] = enum("0", ["0", "1"])
    device.attributes["parameters"]["screenDisplayStatus"] = "0"
    device.commands["settings"] = HonCommand("settings", schema, connection, device)
    device.commands["stopProgram"] = HonCommand(
        "stopProgram", command_schema(), connection, device
    )
    before = getattr(climate, attribute)
    climate.async_write_ha_state = Mock()
    http.post(SEND_URL, payload={"payload": {"resultCode": "0" if accepted else "1"}})
    if accepted:
        await getattr(climate, method)(**args)
    else:
        with pytest.raises(HomeAssistantError):
            await getattr(climate, method)(**args)
    assert getattr(climate, attribute) == before
    assert climate._watcher is None


@pytest.mark.parametrize(
    "method,args",
    [
        ("async_set_sleep_mode", {"sleep_mode": True}),
        ("async_set_rapid_mode", {"rapid_mode": True}),
        ("async_set_silent_mode", {"silent_mode": True}),
        ("async_set_echo_mode", {"echo_mode": False}),
        ("async_set_wind_direction_horizontal", {"value": 7}),
        ("async_set_wind_direction_vertical", {"value": 8}),
        ("async_set_eco_pilot_mode", {"value": "1"}),
        ("async_set_swing_mode", {"swing_mode": "both"}),
        ("async_turn_on", {}),
    ],
)
async def test_ancillary_ack_does_not_change_reported_state(
    hass, entry, device, connection, http, method, args
):
    climate = make_climate(hass, entry, device, connection)
    schema = command_schema()
    for key in ["silentSleepStatus", "rapidMode", "muteStatus", "humanSensingStatus"]:
        schema["parameters"][key] = enum("0", ["0", "1"])
        device.attributes["parameters"][key] = "0"
    device.commands["settings"] = HonCommand("settings", schema, connection, device)
    start = HonCommand("startProgram", command_schema(), connection, device)
    start._multi = {"iot_simple_start": start}
    device.commands["startProgram"] = start
    device.attributes["parameters"]["onOffStatus"] = "0"
    climate._handle_coordinator_update(False)
    climate.hass = hass
    before = (climate.hvac_mode, climate.swing_mode, dict(climate.state_attributes))
    climate.async_write_ha_state = Mock()
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}})
    await getattr(climate, method)(**args)
    assert (
        climate.hvac_mode,
        climate.swing_mode,
        dict(climate.state_attributes),
    ) == before
    assert climate._watcher is None


@pytest.mark.parametrize(
    "key,value",
    [
        ("tempIndoor", None),
        ("tempSel", "bad"),
        ("tempIndoor", "nan"),
        ("onOffStatus", None),
        ("machMode", "unknown"),
    ],
)
async def test_incomplete_climate_telemetry_is_unavailable_not_fake_off(
    hass, entry, device, connection, key, value
):
    climate = make_climate(hass, entry, device, connection)
    device.coordinator.last_update_success = True
    device.attributes["parameters"][key] = value
    climate._handle_coordinator_update(False)
    assert not climate.available
    if key == "tempIndoor":
        assert climate.current_temperature is None
    if key in ("onOffStatus", "machMode"):
        assert climate.hvac_mode is None


@pytest.mark.parametrize("reported", [None, "bad", 99])
async def test_builder_cannot_send_default_for_bad_basis(device, connection, reported):
    install_settings(device, connection)
    device.attributes["parameters"]["tempSel"] = reported
    with pytest.raises(ValueError):
        device.settings_command({"windSpeed": "3"})
    draft = device.settings_command({"tempSel": 23})
    assert draft.parameters["tempSel"].value == 23


async def test_builder_requires_reported_basis_for_unspecified_fields(
    device, connection
):
    install_settings(device, connection)
    device.attributes["parameters"].pop("tempSel")
    with pytest.raises(ValueError):
        device.settings_command({"windSpeed": "3"})
    assert device.settings_command({"tempSel": 23}).parameters["tempSel"].value == 23


async def test_device_send_order_survives_delayed_session_check(device, connection):
    import asyncio

    install_settings(device, connection)
    entered, release = asyncio.Event(), asyncio.Event()
    checks, order = [], []

    async def ensure():
        checks.append(True)
        if len(checks) == 1:
            entered.set()
            await release.wait()

    async def send(command, **kwargs):
        order.append(command["parameters"]["tempSel"])
        return True

    connection._ensure_session = ensure
    connection._send = send
    first = asyncio.create_task(device.settings_command({"tempSel": 23}).send())
    await entered.wait()
    second = asyncio.create_task(
        connection.async_set(device.mac_address, "AC", {"tempSel": 24})
    )
    await asyncio.sleep(0)
    assert len(checks) == 1
    release.set()
    await asyncio.gather(first, second)
    assert order == [23, 24]


@pytest.mark.parametrize("quiet", [False, True])
@pytest.mark.parametrize("service", ["start_program", "update_settings"])
async def test_existing_raw_service_batches_mode_target_health_once(
    hass, entry, loaded_entry, device, connection, http, quiet, service
):
    from homeassistant.helpers import device_registry, entity_registry

    device_registry.async_setup(hass)
    await device_registry.async_load(hass)
    await entity_registry.async_load(hass)
    target = device_registry.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("hon", device.mac_address, "AC")}
    )
    await hass.data["hon"][entry.unique_id].async_close()
    hass.data["hon"][entry.unique_id] = connection
    connection._coordinator_dict[device.mac_address] = device.coordinator
    hass.config_entries.async_update_entry(entry, options={"quiet_ac_commands": quiet})
    schema = command_schema()
    schema["parameters"].update(
        machMode=enum("4", ["1", "4"]), healthMode=enum("0", ["0", "1"])
    )
    device.attributes["parameters"]["healthMode"] = "0"
    for operation in ["startProgram", "settings"]:
        command = HonCommand(operation, schema, connection, device)
        command._multi = {"iot_cool": command} if operation == "startProgram" else {}
        device.commands[operation] = command
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}})
    before = deepcopy(device.attributes)
    await hass.services.async_call(
        "hon",
        service,
        {
            "device_id": [target.id],
            "program": "iot_cool",
            "parameters": {"machMode": "1", "tempSel": 23, "healthMode": "1"},
        },
        blocking=True,
    )
    assert len(sent(http)) == 1
    assert sent(http)[0]["parameters"] | {} == {
        **{
            key: value.value
            for key, value in device.settings_command().parameters.items()
        },
        "machMode": "1",
        "tempSel": 23,
        "healthMode": "1",
        "echoStatus": "1" if quiet else "0",
    }
    assert device.attributes == before


@pytest.mark.parametrize(
    "updates", [{"tempSel": 23, "echoStatus": "1"}, {"tempSel": 23, "echoStatus": 1}]
)
async def test_fixed_conflict_rejected_atomically(device, connection, updates):
    device.commands["settings"] = HonCommand(
        "settings", command_schema("fixed"), connection, device
    )
    before = deepcopy(device.attributes)
    with pytest.raises(ValueError):
        device.settings_command(updates)
    assert device.attributes == before
    assert device.commands["settings"].parameters["tempSel"].value == 21


async def test_off_plus_temperature_is_rejected_but_temperature_only_keeps_off(
    hass, entry, device, connection, http
):
    climate = make_climate(hass, entry, device, connection)
    device.attributes["parameters"]["onOffStatus"] = "0"
    with pytest.raises(ValueError):
        await climate.async_set_temperature(hvac_mode="off", temperature=23)
    assert not http.requests
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}})
    await climate.async_set_temperature(temperature=23)
    assert sent(http)[0]["parameters"]["onOffStatus"] == "0"


async def test_missing_binary_telemetry_is_unknown_not_closed(device):
    from custom_components.hon.base import HonBaseBinarySensorEntity

    sensor = HonBaseBinarySensorEntity(
        device.coordinator, device.appliance, "doorStatus", "Door"
    )
    assert sensor.is_on is None


async def test_missing_water_heater_telemetry_is_unknown(hass, entry, device):
    from custom_components.hon.water_heater import HonWaterHeaterEntity

    device._attributes = {"parameters": {}}
    entity = HonWaterHeaterEntity(hass, device.coordinator, entry, device.appliance)
    assert entity.current_temperature is None
    assert entity.target_temperature is None
    assert entity.current_operation is None


async def test_climate_rejects_program_conflicting_with_requested_mode(
    hass, entry, device, connection, http
):
    climate = make_climate(hass, entry, device, connection)
    schema = command_schema()
    schema["parameters"]["machMode"] = {"typology": "fixed", "fixedValue": "4"}
    command = HonCommand("startProgram", schema, connection, device)
    command._multi = {"iot_cool": command}
    device.commands["startProgram"] = command
    with pytest.raises(ValueError):
        await climate.async_set_temperature(hvac_mode="cool", temperature=23)
    assert not http.requests


async def test_climate_off_sends_off_not_reported_on(
    hass, entry, device, connection, http
):
    climate = make_climate(hass, entry, device, connection)
    device.commands["stopProgram"] = HonCommand(
        "stopProgram", command_schema(), connection, device
    )
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}})
    await climate.async_turn_off()
    assert sent(http)[0]["parameters"]["onOffStatus"] == "0"


async def test_temperature_only_cannot_start_fixed_on_settings(
    hass, entry, device, connection, http
):
    climate = make_climate(hass, entry, device, connection)
    schema = command_schema()
    schema["parameters"]["onOffStatus"] = {"typology": "fixed", "fixedValue": "1"}
    device.commands["settings"] = HonCommand("settings", schema, connection, device)
    device.attributes["parameters"]["onOffStatus"] = "0"
    before = deepcopy(device.attributes)
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}})
    await climate.async_set_temperature(temperature=23)
    assert len(sent(http)) == 1
    assert sent(http)[0]["parameters"]["onOffStatus"] == "0"
    assert sent(http)[0]["parameters"]["tempSel"] == 23
    assert device.attributes == before


@pytest.mark.parametrize(
    "method,args",
    [
        ("async_set_fan_mode", {"fan_mode": "invalid"}),
        ("async_set_swing_mode", {"swing_mode": "invalid"}),
    ],
)
async def test_invalid_climate_enum_has_no_fallback(
    hass, entry, device, connection, http, method, args
):
    climate = make_climate(hass, entry, device, connection)
    with pytest.raises(ValueError):
        await getattr(climate, method)(**args)
    assert not http.requests


async def test_legacy_rejection_does_not_write_reported_sensor(
    hass, entry, loaded_entry, device, connection, http
):
    from homeassistant.helpers import device_registry, entity_registry

    device_registry.async_setup(hass)
    await device_registry.async_load(hass)
    await entity_registry.async_load(hass)
    target = device_registry.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("hon", device.mac_address, "AC")}
    )
    entity = entity_registry.async_get(hass).async_get_or_create(
        "binary_sensor", "hon", device.mac_address + "_health_mode", device_id=target.id
    )
    hass.states.async_set(entity.entity_id, "off")
    connection._coordinator_dict[device.mac_address] = device.coordinator
    hass.data["hon"]["second-account"] = connection
    http.post(SEND_URL, payload={"payload": {"resultCode": "1"}})
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "hon", "climate_turn_health_mode_on", {"device": target.id}, blocking=True
        )
    assert hass.states.get(entity.entity_id).state == "off"


async def test_unknown_service_target_fails_without_attribute_error(
    hass, entry, loaded_entry, http
):
    from homeassistant.helpers import device_registry, entity_registry

    device_registry.async_setup(hass)
    await device_registry.async_load(hass)
    await entity_registry.async_load(hass)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "hon",
            "update_settings",
            {"device_id": ["missing"], "parameters": {}},
            blocking=True,
        )
    assert all(str(url) != SEND_URL for method, url in http.requests)


async def test_builder_rejects_stale_snapshot_after_failed_poll(device, connection):
    install_settings(device, connection)
    device.coordinator.last_update_success = False
    with pytest.raises(ValueError):
        device.settings_command({"tempSel": 23})


async def test_climate_can_initialize_with_incomplete_telemetry(
    hass, entry, device, connection
):
    device._attributes = {"parameters": {"tempIndoor": "20"}}
    climate = make_climate(hass, entry, device, connection)
    assert not climate.available
    assert climate.target_temperature is None
    assert climate.hvac_mode is None


async def test_climate_turn_on_explicitly_requests_power(
    hass, entry, device, connection, http
):
    climate = make_climate(hass, entry, device, connection)
    command = HonCommand("startProgram", command_schema(), connection, device)
    command._multi = {"iot_simple_start": command}
    device.commands["startProgram"] = command
    device.attributes["parameters"]["onOffStatus"] = "0"
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}})
    await climate.async_turn_on()
    assert sent(http)[0]["parameters"]["onOffStatus"] == "1"


@pytest.mark.parametrize(
    "method,args",
    [
        ("async_set_temperature", {"temperature": 23}),
        ("async_set_operation_mode", {"operation_mode": "off"}),
        ("async_turn_off", {}),
        ("async_turn_on", {}),
    ],
)
async def test_water_heater_ack_does_not_replace_telemetry(
    hass, entry, device, connection, http, method, args
):
    from custom_components.hon.water_heater import HonWaterHeaterEntity

    install_settings(device, connection)
    device.attributes["parameters"].update(temp="20", machMode="1")
    device.commands["stopProgram"] = HonCommand(
        "stopProgram", command_schema(), connection, device
    )
    command = HonCommand("startProgram", command_schema(), connection, device)
    command._multi = {"eco": command}
    device.commands["startProgram"] = command
    entity = HonWaterHeaterEntity(hass, device.coordinator, entry, device.appliance)
    entity.async_write_ha_state = Mock()
    before = (entity.current_operation, entity.target_temperature)
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}})
    await getattr(entity, method)(**args)
    assert (entity.current_operation, entity.target_temperature) == before
    assert entity._watcher is None


async def test_water_heater_does_not_truncate_invalid_requested_target(
    hass, entry, device, connection, http
):
    from custom_components.hon.water_heater import HonWaterHeaterEntity

    install_settings(device, connection)
    entity = HonWaterHeaterEntity(hass, device.coordinator, entry, device.appliance)
    with pytest.raises(ValueError):
        await entity.async_set_temperature(temperature=23.9)
    assert not http.requests


def rng(minimum="0", maximum="1", step="0.1", default="0"):
    return {
        "typology": "range",
        "minimumValue": minimum,
        "maximumValue": maximum,
        "incrementValue": step,
        "defaultValue": default,
    }


@pytest.mark.parametrize(
    "schema,value,expected",
    [
        (rng("1", "9", "2", "1"), 3, 3),
        (rng(), 0.3, 0.3),
        (rng(), "0,3", 0.3),
        (rng("-1", "1", "0.2", "-1"), -0.4, -0.4),
    ],
)
def test_F10_valid_minimum_relative_steps(schema, value, expected):
    parameter = HonParameterRange("x", schema)
    parameter.value = value
    assert parameter.value == expected


@pytest.mark.parametrize(
    "value", ["21.9", 21.9, True, None, float("nan"), float("inf")]
)
def test_F10_no_truncation_or_nonfinite_values(value):
    parameter = HonParameterRange("x", rng("16", "30", "1", "21"))
    with pytest.raises(ValueError):
        parameter.value = value
    assert parameter.value == 21


@pytest.mark.parametrize("step", ["0", "-1", "nan", "inf"])
def test_F10_invalid_schema_step(step):
    with pytest.raises(ValueError):
        HonParameterRange("x", rng(step=step))
