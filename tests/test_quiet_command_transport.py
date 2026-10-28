"""Exercise actual aiohttp transport; only the remote server is replaced."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import HomeAssistantError
from yarl import URL

from custom_components.hon.climate import HonClimateEntity
from custom_components.hon.command import HonCommand
from custom_components.hon.const import API_URL
from custom_components.hon.switch import HonSwitchEntity, HonSwitchEntityDescription

pytestmark = pytest.mark.usefixtures("unchanged_context")


def enum(default, values):
    return {"typology": "enum", "defaultValue": default, "enumValues": values}


def command_schema(echo="enum"):
    # Representative fixtures, not captured schemas or claims about real models.
    parameters = {
        "tempSel": {
            "typology": "range",
            "minimumValue": "16",
            "maximumValue": "30",
            "incrementValue": "1",
            "defaultValue": "21",
        },
        "onOffStatus": enum("1", ["0", "1"]),
        "windSpeed": enum("2", ["1", "2", "3", "5"]),
        "windDirectionHorizontal": enum("0", ["0", "7"]),
        "windDirectionVertical": enum("5", ["5", "8"]),
    }
    if echo == "enum":
        parameters["echoStatus"] = enum("0", ["0", "1"])
    elif echo == "fixed":
        parameters["echoStatus"] = {"typology": "fixed", "fixedValue": "0"}
    return {
        "parameters": parameters,
        "ancillaryParameters": {
            "programName": {"typology": "fixed", "fixedValue": "PROGRAMS.AC.TEST"}
        },
    }


SEND_URL = f"{API_URL}/commands/v1/send"
DEVICE = SimpleNamespace(
    mac_address="00:00:00:00:00:01", appliance_type="AC", commands_options={}
)


def sent(http):
    return [call.kwargs["json"] for call in http.requests[("POST", URL(SEND_URL))]]


async def test_default_preserves_transport_payload(connection, http):
    parameters = {"echoStatus": "0", "tempSel": "21"}
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}})
    assert await connection.send_command(DEVICE, "settings", parameters, {}) is True
    assert sent(http)[0]["parameters"] == parameters


async def test_quiet_settings_override_at_http_boundary(hass, entry, connection, http):
    hass.config_entries.async_update_entry(entry, options={"quiet_ac_commands": True})
    parameters = {"echoStatus": "0", "tempSel": "21"}
    original = deepcopy(parameters)
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}})
    assert await connection.send_command(DEVICE, "settings", parameters, {}) is True
    assert sent(http)[0]["parameters"] == {"echoStatus": "1", "tempSel": "21"}
    assert parameters == original
    assert sum(len(calls) for calls in http.requests.values()) == 1


async def test_quiet_low_level_override_at_http_boundary(hass, entry, connection, http):
    hass.config_entries.async_update_entry(entry, options={"quiet_ac_commands": True})
    parameters = {"echoStatus": "0", "onOffStatus": "0"}
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}})
    assert await connection.async_set(DEVICE.mac_address, "AC", parameters) is True
    assert sent(http)[0]["parameters"] == {"echoStatus": "1", "onOffStatus": "0"}
    assert parameters == {"echoStatus": "0", "onOffStatus": "0"}
    assert sum(len(calls) for calls in http.requests.values()) == 1


@pytest.mark.parametrize(
    "operation,echo,updates",
    [
        ("startProgram", "omitted", {"onOffStatus": "1"}),
        ("stopProgram", "fixed", {"onOffStatus": "0"}),
        ("settings", "enum", {"tempSel": "23", "echoStatus": "0"}),
        ("settings", "fixed", {"windSpeed": "3"}),
        (
            "settings",
            "enum",
            {"windDirectionHorizontal": "7", "windDirectionVertical": "8"},
        ),
    ],
    ids=[
        "start-omitted",
        "stop-fixed",
        "temperature-explicit-zero",
        "fan-fixed",
        "swing-enum",
    ],
)
async def test_real_builder_to_transport_preserves_everything_except_echo(
    hass, entry, connection, device, http, operation, echo, updates
):
    hass.config_entries.async_update_entry(entry, options={"quiet_ac_commands": True})
    schema = command_schema(echo)
    original_schema = deepcopy(schema)
    device.commands[operation] = HonCommand(operation, schema, connection, device)
    builder = {
        "startProgram": device.start_command,
        "stopProgram": device.stop_command,
        "settings": device.settings_command,
    }[operation]
    if operation == "startProgram":
        command = builder(parameters=updates)
    else:
        command = builder(updates)
    before = {key: parameter.value for key, parameter in command.parameters.items()}
    shadow_before_send = deepcopy(device.attributes)
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}})
    assert await command.send() is True
    payload = sent(http)[0]
    assert payload["commandName"] == operation
    assert payload["parameters"] == {**before, "echoStatus": "1"}
    for key, value in updates.items():
        if key != "echoStatus":
            assert str(payload["parameters"][key]) == value
    assert payload["ancillaryParameters"] == command.ancillary_parameters
    assert {
        key: parameter.value for key, parameter in command.parameters.items()
    } == before
    assert device.attributes == shadow_before_send
    assert schema == original_schema
    assert sum(len(calls) for calls in http.requests.values()) == 2
    assert len(sent(http)) == 1  # One POST plus readback, never a replay.


@pytest.mark.parametrize("low_level", [False, True])
@pytest.mark.parametrize(
    "appliance_type,options",
    [
        ("AC", {}),
        ("AC", {"quiet_ac_commands": False}),
        ("WM", {"quiet_ac_commands": True}),
    ],
)
async def test_disabled_and_non_ac_payloads_unchanged(
    hass, entry, connection, http, low_level, appliance_type, options
):
    hass.config_entries.async_update_entry(entry, options=options)
    parameters = {"echoStatus": "0", "onOffStatus": "1"}
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}})
    if low_level:
        assert (
            await connection.async_set(DEVICE.mac_address, appliance_type, parameters)
            is True
        )
    else:
        device = SimpleNamespace(
            mac_address=DEVICE.mac_address,
            appliance_type=appliance_type,
            commands_options={},
        )
        assert await connection.send_command(device, "settings", parameters, {}) is True
    assert sent(http)[0]["parameters"] == parameters
    assert len(sent(http)) == 1


@pytest.mark.parametrize(
    "low_level", [False, True], ids=["builder-transport", "low-level"]
)
async def test_quiet_rejection_is_visible_without_noisy_retry(
    hass, entry, connection, http, low_level
):
    hass.config_entries.async_update_entry(entry, options={"quiet_ac_commands": True})
    http.post(SEND_URL, payload={"payload": {"resultCode": "1"}})
    with pytest.raises(HomeAssistantError, match="quiet AC command"):
        if low_level:
            await connection.async_set(DEVICE.mac_address, "AC", {"echoStatus": "0"})
        else:
            await connection.send_command(DEVICE, "settings", {"echoStatus": "0"}, {})
    assert len(sent(http)) == 1
    assert sent(http)[0]["parameters"]["echoStatus"] == "1"


@pytest.mark.parametrize("low_level", [False, True])
@pytest.mark.parametrize("appliance_type,quiet", [("AC", False), ("WM", True)])
async def test_rejection_raises_when_policy_does_not_apply(
    hass, entry, connection, http, low_level, appliance_type, quiet
):
    hass.config_entries.async_update_entry(entry, options={"quiet_ac_commands": quiet})
    http.post(SEND_URL, payload={"payload": {"resultCode": "1"}})
    with pytest.raises(HomeAssistantError, match="hOn rejected"):
        if low_level:
            await connection.async_set(DEVICE.mac_address, appliance_type, {})
        else:
            device = SimpleNamespace(
                mac_address=DEVICE.mac_address,
                appliance_type=appliance_type,
                commands_options={},
            )
            await connection.send_command(device, "settings", {}, {})
    assert len(sent(http)) == 1


async def test_climate_rejection_propagates_before_reporting_target_success(
    hass, entry, connection, device, http
):
    hass.config_entries.async_update_entry(entry, options={"quiet_ac_commands": True})
    hass.data["hon"] = {entry.unique_id: connection}
    device.commands["settings"] = HonCommand(
        "settings", command_schema(), connection, device
    )
    climate = HonClimateEntity(hass, device.coordinator, entry, device.appliance)
    original_target = climate.target_temperature
    http.post(SEND_URL, payload={"payload": {"resultCode": "1"}})
    with pytest.raises(HomeAssistantError, match="quiet AC command"):
        await climate.async_set_temperature(temperature=23)
    assert climate.target_temperature == original_target
    assert climate._watcher is None
    assert len(sent(http)) == 1


async def test_switch_low_level_rejection_propagates_before_optimistic_update(
    hass, entry, connection, device, http
):
    hass.config_entries.async_update_entry(entry, options={"quiet_ac_commands": True})
    device.attributes["parameters"]["screenDisplayStatus"] = "0"
    description = HonSwitchEntityDescription(
        key="screenDisplayStatus", name="Display", icon="mdi:monitor"
    )
    switch = HonSwitchEntity(
        hass, device.coordinator, entry, device.appliance, description
    )
    http.post(SEND_URL, payload={"payload": {"resultCode": "1"}})
    with pytest.raises(HomeAssistantError, match="quiet AC command"):
        await switch.async_turn_on()
    assert device.get("screenDisplayStatus") == "0"
    assert switch.is_on is False
    assert len(sent(http)) == 1


async def test_poll_reauth_and_live_option_changes_do_not_reset_policy(
    hass, entry, connection, device, http, auth_http
):
    hass.config_entries.async_update_entry(entry, options={"quiet_ac_commands": True})
    device.commands["settings"] = HonCommand(
        "settings", command_schema(), connection, device
    )
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}}, repeat=True)
    assert await device.settings_command({"tempSel": "22"}).send()
    http.get(
        f"{API_URL}/commands/v1/context?macAddress={device.mac_address}&applianceType=AC&category=CYCLE",
        payload={
            # A settings command requires a complete reported base, not defaults.
            "payload": {"shadow": {"parameters": {
                key: {"parNewVal": value}
                for key, value in device.attributes["parameters"].items()
            }}}
        },
    )
    await device.load_context()
    assert device.get("echoStatus") == "0"
    connection._start_time = (
        0  # Expired session: real _ensure_session + mocked HTTP auth.
    )
    assert await device.settings_command({"tempSel": "23"}).send()
    assert entry.options == {"quiet_ac_commands": True}
    hass.config_entries.async_update_entry(entry, options={"quiet_ac_commands": False})
    assert await device.settings_command({"tempSel": "24"}).send()
    assert [payload["parameters"]["echoStatus"] for payload in sent(http)] == [
        "1",
        "1",
        "0",
    ]


@pytest.mark.parametrize("low_level", [False, True])
@pytest.mark.parametrize(
    "response",
    [
        {"status": 503, "payload": {"payload": {"resultCode": "0"}}},
        {"status": 200, "body": "not json"},
    ],
)
async def test_quiet_transport_failure_never_reports_success(
    hass, entry, connection, http, low_level, response
):
    hass.config_entries.async_update_entry(entry, options={"quiet_ac_commands": True})
    http.post(SEND_URL, **response)
    with pytest.raises(HomeAssistantError, match="quiet AC command"):
        if low_level:
            await connection.async_set(DEVICE.mac_address, "AC", {"echoStatus": "0"})
        else:
            await connection.send_command(DEVICE, "settings", {"echoStatus": "0"}, {})
    assert len(sent(http)) == 1
