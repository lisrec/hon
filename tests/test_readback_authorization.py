"""Per-device authorization; promoted independent B1/B2, real HA + offline HTTP."""
import asyncio
from copy import deepcopy

import pytest
from aioresponses import CallbackResult

from custom_components.hon.command import HonCommand
from custom_components.hon import command as module
from test_command_readback import registered_climate, snapshot, CONTEXT_URL
from test_quiet_command_transport import SEND_URL, sent


def gets(http):
    return sum(len(v) for (method, _), v in http.requests.items() if method == "GET")


async def test_exhausted_stale_must_not_authorize_destructive_next_settings(
    hass, device, http, registered_climate
):
    """B1: accepted fan change cannot be silently undone by swing."""
    device.attributes["parameters"].update(onOffStatus="1", machMode="1")
    before = deepcopy(device.attributes["parameters"])
    assert before["windSpeed"] == "5"
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}}, repeat=True)
    http.get(CONTEXT_URL, payload=snapshot(before), repeat=True)
    await hass.services.async_call("climate", "set_fan_mode", {
        "entity_id": registered_climate.entity_id, "fan_mode": "low",
    }, blocking=True)
    assert sent(http)[0]["parameters"]["windSpeed"] == "3"
    await hass.services.async_call("climate", "set_swing_mode", {
        "entity_id": registered_climate.entity_id, "swing_mode": "both",
    }, blocking=True)
    assert len(sent(http)) == 1
    assert gets(http) == 4
    assert device.attributes["parameters"] == before


async def test_concurrent_setters_must_not_revert_pending_fan(
    hass, device, http, registered_climate
):
    """B2: the second real setter cannot authorize a concurrent stale draft."""
    device.attributes["parameters"].update(onOffStatus="1", machMode="1")
    before = deepcopy(device.attributes["parameters"])
    first_get = asyncio.Event()

    def get(url, **kwargs):
        first_get.set()
        return CallbackResult(payload=snapshot(before))

    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}}, repeat=True)
    http.get(CONTEXT_URL, callback=get, repeat=True)
    fan = asyncio.create_task(hass.services.async_call("climate", "set_fan_mode", {
        "entity_id": registered_climate.entity_id, "fan_mode": "low",
    }, blocking=True))
    await first_get.wait()
    try:
        await hass.services.async_call("climate", "set_swing_mode", {
            "entity_id": registered_climate.entity_id, "swing_mode": "both",
        }, blocking=True)
        await fan
    finally:
        if not fan.done():
            fan.cancel()
            await asyncio.gather(fan, return_exceptions=True)
    assert len(sent(http)) == 1
    assert gets(http) == 4
    assert device.attributes["parameters"] == before


async def test_prebuilt_draft_is_not_authorized_after_matching_readback(
    device, http, registered_climate
):
    """A matching GET cannot retroactively update an already-built payload."""
    old = device.settings_command({"windDirectionHorizontal": "7"})
    report = {**device.attributes["parameters"], "windSpeed": "3"}
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}}, repeat=True)
    http.get(CONTEXT_URL, payload=snapshot(report), repeat=True)
    assert await device.settings_command({"windSpeed": "3"}).send() is True
    assert await old.send() is False
    assert len(sent(http)) == 1
    assert old.parameters["windSpeed"].value == "5"
    assert device.attributes["parameters"] == report
    # A later genuine report may change again (e.g. another remote controller).
    # Once confirmed, accepted intent must not become a permanent desired state.
    http.get(CONTEXT_URL, payload=snapshot({**report, "windSpeed": "5"}))
    # Remove the earlier repeating matcher, keeping request history intact.
    http.clear()
    http.get(CONTEXT_URL, payload=snapshot({**report, "windSpeed": "5"}))
    await device.coordinator.async_refresh()
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}})
    http.get(CONTEXT_URL, payload=snapshot({**report, "windSpeed": "5", "windDirectionHorizontal": "7"}))
    assert await device.settings_command({"windDirectionHorizontal": "7"}).send() is True
    assert len(sent(http)) == 2
    assert sent(http)[1]["parameters"]["windSpeed"] == "5"


async def test_concurrent_draft_before_post_acceptance_is_withheld(
    device, http, registered_climate
):
    entered = asyncio.Event()
    release = asyncio.Event()
    before = deepcopy(device.attributes["parameters"])

    async def post(url, **kwargs):
        entered.set()
        await release.wait()
        return CallbackResult(payload={"payload": {"resultCode": "0"}})

    http.post(SEND_URL, callback=post, repeat=True)
    http.get(CONTEXT_URL, payload=snapshot({**before, "windSpeed": "3"}), repeat=True)
    first = asyncio.create_task(device.settings_command({"windSpeed": "3"}).send())
    await entered.wait()
    second = asyncio.create_task(device.settings_command({"windDirectionHorizontal": "7"}).send())
    await asyncio.sleep(0)
    try:
        assert not second.done()
        assert len(sent(http)) == 1
    finally:
        release.set()
        results = await asyncio.gather(first, second)
    assert results == [True, False]
    assert len(sent(http)) == 1


async def test_concurrent_draft_during_get_cannot_use_old_basis_after_confirmation(
    device, http, registered_climate
):
    entered = asyncio.Event()
    release = asyncio.Event()
    before = deepcopy(device.attributes["parameters"])

    async def get(url, **kwargs):
        entered.set()
        await release.wait()
        return CallbackResult(payload=snapshot({**before, "windSpeed": "3"}))

    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}}, repeat=True)
    http.get(CONTEXT_URL, callback=get, repeat=True)
    first = asyncio.create_task(device.settings_command({"windSpeed": "3"}).send())
    await entered.wait()
    stale = device.settings_command({"windDirectionHorizontal": "7"})
    second = asyncio.create_task(stale.send())
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second)
    assert results == [True, False]
    assert len(sent(http)) == 1
    assert device.attributes["parameters"] == {**before, "windSpeed": "3"}


async def test_eventual_real_report_recovers_without_replay_or_overlay(
    device, http, registered_climate, monkeypatch
):
    monkeypatch.setattr(module, "_COMMAND_TIMEOUT", 0.05)
    before = deepcopy(device.attributes["parameters"])
    source = device.commands["settings"]
    schema = {k: deepcopy(vars(v)) for k, v in source.parameters.items()}
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}}, repeat=True)
    http.get(CONTEXT_URL, payload=snapshot(before), repeat=True)
    assert await device.settings_command({"windSpeed": "3"}).send() is True
    assert not device.coordinator.last_update_success
    assert device.attributes["parameters"] == before
    # A successful but still stale ordinary GET must not clear the fence.
    await device.coordinator.async_refresh()
    assert device.coordinator.last_update_success
    assert await device.settings_command({"windDirectionHorizontal": "7"}).send() is False
    assert len(sent(http)) == 1
    assert {k: vars(v) for k, v in source.parameters.items()} == schema
    http.clear()
    fan_report = {**before, "windSpeed": "3"}
    http.get(CONTEXT_URL, payload=snapshot(fan_report))
    await device.coordinator.async_refresh()
    assert device.attributes["parameters"] == fan_report
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}})
    http.get(CONTEXT_URL, payload=snapshot({**fan_report, "windDirectionHorizontal": "7"}))
    assert await device.settings_command({"windDirectionHorizontal": "7"}).send() is True
    assert len(sent(http)) == 2  # Exactly one new intent, no accepted POST replay.
    assert sent(http)[1]["parameters"]["windSpeed"] == "3"
    assert {k: vars(v) for k, v in source.parameters.items()} == schema


@pytest.mark.parametrize("dispatch", [False, True], ids=["unavailable-direct", "stale-native-service"])
async def test_neutral_off_remains_available_after_failed_readback(
    hass, device, connection, http, registered_climate, dispatch
):
    before = deepcopy(device.attributes["parameters"])
    device.commands["stopProgram"] = HonCommand("stopProgram", {"parameters": {
        "onOffStatus": {"typology": "fixed", "fixedValue": "0"},
    }}, connection, device)
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}}, repeat=True)
    http.get(CONTEXT_URL, status=503)
    assert await device.settings_command({"windSpeed": "3"}).send() is True
    assert not registered_climate.available
    assert device.attributes["parameters"] == before
    if dispatch:
        # Core skips native services for unavailable entities. A successful but
        # stale poll restores transport availability, not accepted-intent basis.
        http.get(CONTEXT_URL, payload=snapshot(before))
        await device.coordinator.async_refresh()
        assert registered_climate.available
    http.get(CONTEXT_URL, payload=snapshot({**before, "onOffStatus": "0"}))
    if dispatch:
        await hass.services.async_call("climate", "turn_off", {
            "entity_id": registered_climate.entity_id,
        }, blocking=True)
    else:
        await registered_climate.async_turn_off()
    assert len(sent(http)) == 2
    assert sent(http)[1]["commandName"] == "stopProgram"
    assert sent(http)[1]["parameters"] == {"onOffStatus": "0"}
    assert device.coordinator.last_update_success
    assert registered_climate.available
    # OFF acknowledges only power, not the earlier unresolved fan intent.
    assert await device.settings_command({"windDirectionHorizontal": "7"}).send() is False
    assert len(sent(http)) == 2


@pytest.mark.parametrize("field,next_field", [("rapidMode", "muteStatus"), ("muteStatus", "rapidMode")])
async def test_actual_preset_fields_cannot_be_reverted_by_next_settings(
    device, http, registered_climate, field, next_field
):
    before = deepcopy(device.attributes["parameters"])
    assert before[field] == "0"
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}}, repeat=True)
    http.get(CONTEXT_URL, payload=snapshot(before), repeat=True)
    assert await device.settings_command({field: "1"}).send() is True
    assert str(sent(http)[0]["parameters"][field]) == "1"
    assert await device.settings_command({next_field: "1"}).send() is False
    assert len(sent(http)) == 1
    assert device.attributes["parameters"] == before


async def test_full_settings_disguised_as_stop_cannot_bypass_pending_fence(
    device, connection, http, registered_climate, monkeypatch
):
    monkeypatch.setattr(module, "_COMMAND_TIMEOUT", 0.05)
    before = deepcopy(device.attributes["parameters"])
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}}, repeat=True)
    http.get(CONTEXT_URL, payload=snapshot(before), repeat=True)
    assert await device.settings_command({"windSpeed": "3"}).send() is True
    await device.coordinator.async_refresh()
    schema = {"parameters": {
        "onOffStatus": {"typology": "fixed", "fixedValue": "0"},
        "windSpeed": {"typology": "fixed", "fixedValue": "5"},
    }}
    device.commands["stopProgram"] = HonCommand("stopProgram", schema, connection, device)
    assert await device.stop_command().send() is False
    assert len(sent(http)) == 1
