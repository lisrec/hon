"""Offline outbound-request proof, NOT evidence of firmware acceptance."""
import asyncio
import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest
from aioresponses import CallbackResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed
from test_command_readback import CONTEXT_URL, snapshot
from test_command_readback import registered_climate as registered_climate  # noqa: PLC0414 -- pytest fixture re-export
from test_neutral_off import STOPS
from test_quiet_command_transport import SEND_URL, sent

from custom_components.hon.command import HonCommand
from custom_components.hon.parameter import HonParameterFixed

GOLDEN = json.loads((Path(__file__).parent / "fixtures/generic_ac_off_request.json").read_text())["request"]


def stable(request):
    request = deepcopy(request)
    stamp = request.pop("timestamp")
    datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
    assert request.pop("transactionId") == request["macAddress"] + "_" + stamp
    return request


@pytest.mark.parametrize("service,data", [
    ("turn_off", {}), ("set_hvac_mode", {"hvac_mode": "off"}),
])
@pytest.mark.parametrize("mode", ["2", "4"])
@pytest.mark.parametrize("quiet", [False, True])
async def test_generic_off_exact_request_and_transaction(
    hass, entry, device, connection, http, registered_climate, service, data, mode, quiet
):
    hass.config_entries.async_update_entry(entry, options={"quiet_ac_commands": quiet})
    device.commands["stopProgram"] = HonCommand("stopProgram", deepcopy(STOPS[0]["stopProgram"]), connection, device)
    source = device.commands["stopProgram"]
    source_before = deepcopy({k: vars(v) for k, v in source.parameters.items()})
    ancillary_before = deepcopy(source.ancillary_parameters)
    device._attributes["parameters"]["machMode"] = mode
    before = deepcopy(device.attributes)
    pending = {"onOffStatus": "1", "tempSel": "29", "windSpeed": "3", "machMode": mode}
    device._pending_command = deepcopy(pending)
    # Genuine existing coordinator.async_set -> HonConnection.async_set baseline.
    original = {"onOffStatus": "0", "machMode": "1"}
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}})
    await device.coordinator.async_set(original)
    assert original == {"onOffStatus": "0", "machMode": "1"}
    baseline = stable(sent(http)[0])
    expected = deepcopy(GOLDEN)
    if quiet:
        expected["parameters"]["echoStatus"] = "1"
    assert baseline == expected
    generation = device._command_generation
    off = {**before["parameters"], "onOffStatus": "0"}
    events = []

    def post(url, **kwargs):
        assert device._command_transaction.locked()
        assert device.attributes == before
        assert device._pending_command == pending
        assert stable(kwargs["json"]) == baseline
        events.append("POST")
        return CallbackResult(payload={"payload": {"resultCode": "0"}})

    def get(url, **kwargs):
        assert device._command_transaction.locked()
        assert not connection._command_locks[device.mac_address].locked()
        assert device.attributes == before
        assert device._pending_command == {**pending, "onOffStatus": "0"}
        events.append("GET")
        assert device._command_generation == generation + 1
        return CallbackResult(payload=snapshot(off))

    http.post(SEND_URL, callback=post)
    http.get(CONTEXT_URL, callback=get)
    await hass.services.async_call("climate", service, {
        "entity_id": registered_climate.entity_id, **data,
    }, blocking=True)
    assert events == ["POST", "GET"]
    # Accepted POST and successfully parsed context each invalidate old drafts.
    assert device._command_generation == generation + 2
    assert device._pending_command == {**pending, "onOffStatus": "0"}
    assert device.attributes == {"parameters": off}
    assert hass.states.get(registered_climate.entity_id).state == "off"
    assert stable(sent(http)[1]) == expected
    assert {k: vars(v) for k, v in source.parameters.items()} == source_before
    assert source.ancillary_parameters == ancillary_before
    assert source._generic_off is False
    assert len(sent(http)) == 2


@pytest.mark.parametrize("extra", ["machMode", "windSpeed", "tempSel", "rapidMode"])
async def test_transport_marker_never_exempts_mutated_nonpower_draft(
    device, connection, http, registered_climate, extra
):
    device.commands["stopProgram"] = HonCommand("stopProgram", deepcopy(STOPS[0]["stopProgram"]), connection, device)
    draft = device.stop_command()
    draft.parameters[extra] = HonParameterFixed(extra, {"fixedValue": "1"})
    device._pending_command = {"tempSel": "29"}
    assert await draft.send() is False
    assert not http.requests
    assert device._pending_command == {"tempSel": "29"}


@pytest.mark.parametrize("field", ["_generic_off", "generic_off", "transport"])
async def test_service_parameters_cannot_select_generic_transport(device, connection, registered_climate, field):
    device.commands["stopProgram"] = HonCommand("stopProgram", deepcopy(STOPS[0]["stopProgram"]), connection, device)
    with pytest.raises(ValueError, match="Unsupported parameter"):
        device.stop_command({"onOffStatus": "0", field: "1"})


@pytest.mark.parametrize("service,data", [
    ("turn_off", {}), ("set_hvac_mode", {"hvac_mode": "off"}),
])
@pytest.mark.parametrize("failure", ["http", "reject", "invalid"])
async def test_generic_off_send_failure_propagates_without_readback_or_intent_change(
    hass, device, connection, http, registered_climate, service, data, failure
):
    device.commands["stopProgram"] = HonCommand("stopProgram", deepcopy(STOPS[0]["stopProgram"]), connection, device)
    device._pending_command = {"windSpeed": "3"}
    before = deepcopy(device.attributes)
    generation = device._command_generation
    if failure == "http":
        http.post(SEND_URL, status=503)
    else:
        http.post(SEND_URL, payload={"payload": {"resultCode": "1"}} if failure == "reject" else {})
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("climate", service, {
            "entity_id": registered_climate.entity_id, **data,
        }, blocking=True)
    assert stable(sent(http)[0]) == GOLDEN
    assert device.attributes == before
    assert device._pending_command == {"windSpeed": "3"}
    assert device._command_generation == generation
    assert not device._command_transaction.locked()
    assert sum(map(len, http.requests.values())) == 1


@pytest.mark.parametrize("failure", ["failed", "stale", "slow"])
async def test_generic_off_readback_is_real_bounded_and_never_replays(
    hass, device, connection, http, registered_climate, monkeypatch, failure
):
    device.commands["stopProgram"] = HonCommand("stopProgram", deepcopy(STOPS[0]["stopProgram"]), connection, device)
    device._attributes["parameters"]["onOffStatus"] = "1"
    before = deepcopy(device.attributes)
    device._pending_command = {"windSpeed": "3", "machMode": "4"}
    # Build a neutral OFF without a usable coordinator basis, then keep the
    # actual GET failure/staleness separate from POST acceptance.
    device.coordinator.async_set_update_error(UpdateFailed("offline test"))
    draft = device.stop_command()
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}})
    if failure == "failed":
        http.get(CONTEXT_URL, status=503)
    elif failure == "stale":
        http.get(CONTEXT_URL, payload=snapshot(before["parameters"]), repeat=True)
    else:
        async def slow_get(url, **kwargs):
            assert device._command_transaction.locked()
            assert device.attributes == before
            await asyncio.sleep(10)
            return CallbackResult(payload=snapshot({**before["parameters"], "onOffStatus": "0"}))
        monkeypatch.setattr("custom_components.hon.command._COMMAND_TIMEOUT", 0.05)
        http.get(CONTEXT_URL, callback=slow_get)
    async with asyncio.timeout(5):
        assert await draft.send() is True  # Acceptance only, not physical OFF.
    assert device.attributes == before
    assert device._pending_command == {"windSpeed": "3", "machMode": "4", "onOffStatus": "0"}
    assert len(sent(http)) == 1
    assert stable(sent(http)[0]) == GOLDEN
    assert not device._command_transaction.locked()
    assert not connection._command_locks[device.mac_address].locked()
    if failure != "stale":
        assert not device.coordinator.last_update_success
        assert hass.states.get(registered_climate.entity_id).state == "unavailable"
    else:
        assert device.coordinator.last_update_success
        assert sum(map(len, http.requests.values())) == 5  # One POST + four real GETs.
