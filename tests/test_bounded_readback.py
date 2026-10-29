"""Bounded late readback through real HA services/coordinator; offline HTTP only."""
import asyncio
from copy import deepcopy

import pytest
from aioresponses import CallbackResult

from test_command_readback import CONTEXT_URL, registered_climate, snapshot
from test_quiet_command_transport import SEND_URL, sent


async def test_late_start_readback_published_before_next_settings(
    hass, entry, device, connection, http, registered_climate
):
    hass.config_entries.async_update_entry(entry, options={"quiet_ac_commands": True})
    climate = registered_climate
    before = deepcopy(device.attributes["parameters"])
    fresh = {**before, "onOffStatus": "1", "machMode": "1"}
    target = {**fresh, "tempSel": "23.00"}
    events = []

    def post(url, **kwargs):
        events.append(("POST", kwargs["json"]["commandName"]))
        return CallbackResult(payload={"payload": {"resultCode": "0"}})

    def get(report):
        def callback(url, **kwargs):
            assert not connection._command_locks[device.mac_address].locked()
            events.append(("GET", report["onOffStatus"]))
            return CallbackResult(payload=snapshot(report))
        return callback

    http.post(SEND_URL, callback=post, repeat=True)
    http.get(CONTEXT_URL, callback=get(before))
    http.get(CONTEXT_URL, callback=get(fresh))
    http.get(CONTEXT_URL, callback=get(target))
    async with asyncio.timeout(5):
        await hass.services.async_call("climate", "set_hvac_mode", {
            "entity_id": climate.entity_id, "hvac_mode": "cool"
        }, blocking=True)
        assert hass.states.get(climate.entity_id).state == "cool"
        assert device.attributes["parameters"] == fresh
        await hass.services.async_call("climate", "set_temperature", {
            "entity_id": climate.entity_id, "temperature": 23
        }, blocking=True)
        assert hass.states.get(climate.entity_id).attributes["temperature"] == 23
    assert events == [("POST", "startProgram"), ("GET", "0"), ("GET", "1"), ("POST", "settings"), ("GET", "1")]
    payloads = sent(http)
    assert len(payloads) == 2
    assert payloads[1]["parameters"]["onOffStatus"] == "1"
    assert payloads[1]["parameters"]["machMode"] == "1"
    assert all(p["parameters"]["echoStatus"] == "1" for p in payloads)
    assert device.attributes["parameters"]["echoStatus"] == "0"


async def test_readback_deadline_cancels_slow_get_and_publishes_unavailable(
    hass, device, http, registered_climate, monkeypatch
):
    from custom_components.hon import command
    # Shorten the production total budget, not the outer safety assertion.
    monkeypatch.setattr(command, "_COMMAND_TIMEOUT", 0.05, raising=False)
    before = deepcopy(device.attributes)
    get_started = asyncio.Event()
    get_cancelled = asyncio.Event()

    async def slow_get(url, **kwargs):
        get_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            get_cancelled.set()

    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}})
    http.get(CONTEXT_URL, callback=slow_get)
    async with asyncio.timeout(0.5):
        assert await device.settings_command({"tempSel": 23}).send() is True
    assert get_started.is_set()
    assert get_cancelled.is_set()
    assert device.attributes == before
    assert not device.coordinator.last_update_success
    assert hass.states.get(registered_climate.entity_id).state == "unavailable"
    assert len(sent(http)) == 1


@pytest.mark.parametrize("phase", ["get", "delay", "coordinator_lock"])
async def test_external_readback_cancellation_propagates_without_post_replay(
    hass, device, http, registered_climate, phase
):
    before = deepcopy(device.attributes)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def get(url, **kwargs):
        started.set()
        if phase == "get":
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return CallbackResult(payload=snapshot(before["parameters"]))

    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}})
    http.get(CONTEXT_URL, callback=get, repeat=True)
    lock = device.coordinator._debounced_refresh.async_lock()
    if phase == "coordinator_lock":
        await lock.__aenter__()
    task = asyncio.create_task(device.settings_command({"tempSel": 23}).send())
    try:
        async with asyncio.timeout(0.5):
            if phase == "coordinator_lock":
                while not http.requests:
                    await asyncio.sleep(0)
            else:
                await started.wait()
                await asyncio.sleep(0)  # Let the stale GET return into the retry delay.
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        if phase == "coordinator_lock":
            await lock.__aexit__(None, None, None)
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert device.attributes == before
    assert len(sent(http)) == 1
    assert phase != "get" or cancelled.is_set()


@pytest.mark.parametrize("phase", ["delay", "coordinator_lock"])
async def test_total_readback_deadline_includes_delay_and_coordinator_lock(
    hass, device, http, registered_climate, monkeypatch, phase
):
    from custom_components.hon import command
    monkeypatch.setattr(command, "_COMMAND_TIMEOUT", 0.05)
    before = deepcopy(device.attributes)
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}})
    http.get(CONTEXT_URL, payload=snapshot(before["parameters"]), repeat=True)
    lock = device.coordinator._debounced_refresh.async_lock()
    if phase == "coordinator_lock":
        await lock.__aenter__()
    try:
        async with asyncio.timeout(0.5):
            assert await asyncio.create_task(device.settings_command({"tempSel": 23}).send()) is True
    finally:
        if phase == "coordinator_lock":
            await lock.__aexit__(None, None, None)
    assert device.attributes == before
    assert not device.coordinator.last_update_success
    assert hass.states.get(registered_climate.entity_id).state == "unavailable"
    assert len(sent(http)) == 1


@pytest.mark.parametrize("updates", [
    {"windSpeed": "3"},
    {"windDirectionHorizontal": "7", "windDirectionVertical": "8"},
])
async def test_late_settings_readback_uses_reported_fields_not_quiet_echo(
    hass, entry, device, connection, http, registered_climate, updates
):
    hass.config_entries.async_update_entry(entry, options={"quiet_ac_commands": True})
    before = deepcopy(device.attributes["parameters"])
    fresh = {**before, **updates}
    calls = []

    def get(report):
        def callback(url, **kwargs):
            assert not connection._command_locks[device.mac_address].locked()
            assert device.attributes["parameters"] == before
            calls.append(report)
            return CallbackResult(payload=snapshot(report))
        return callback

    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}})
    http.get(CONTEXT_URL, callback=get(before))
    http.get(CONTEXT_URL, callback=get(fresh))
    async with asyncio.timeout(2):
        if "windSpeed" in updates:
            await hass.services.async_call("climate", "set_fan_mode", {
                "entity_id": registered_climate.entity_id, "fan_mode": "low"
            }, blocking=True)
        else:
            await hass.services.async_call("climate", "set_swing_mode", {
                "entity_id": registered_climate.entity_id, "swing_mode": "both"
            }, blocking=True)
    assert len(calls) == 2
    assert device.attributes["parameters"] == fresh
    assert len(sent(http)) == 1
    assert sent(http)[0]["parameters"]["echoStatus"] == "1"
    assert device.attributes["parameters"]["echoStatus"] == "0"
