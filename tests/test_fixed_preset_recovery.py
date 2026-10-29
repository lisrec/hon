"""Real reported preset confirmation releases the B3 fence, never ACK alone."""
from copy import deepcopy

import pytest
from aioresponses import CallbackResult
from test_ac_fan_transition import MODELS, MODEL_IDS, fan_climate, schema_state
from test_command_readback import CONTEXT_URL, snapshot
from test_quiet_command_transport import SEND_URL, sent


@pytest.mark.parametrize("fan_climate", MODELS, ids=MODEL_IDS, indirect=True)
@pytest.mark.parametrize("field", ["rapidMode", "muteStatus"])
async def test_fixed_preset_late_report_recovers_without_replay(
    hass, device, connection, http, fan_climate, field
):
    device.attributes["parameters"][field] = "1"
    device.coordinator.async_update_listeners()
    before = deepcopy(device.attributes["parameters"])
    sources = schema_state(device)
    remote = deepcopy(before)
    gets = []
    controls = {"onOffStatus", "machMode", "tempSel", "windSpeed",
                "windDirectionHorizontal", "windDirectionVertical",
                "rapidMode", "muteStatus"}

    def post(url, **kw):
        assert schema_state(device) == sources
        remote.update({k: str(v) for k, v in kw["json"]["parameters"].items()
                       if k in controls and (len(sent(http)) > 1 or k != field)})
        return CallbackResult(payload={"payload": {"resultCode": "0"}})

    def get(url, **kw):
        assert not connection._command_locks[device.mac_address].locked()
        gets.append(deepcopy(remote))
        return CallbackResult(payload=snapshot(remote))

    http.post(SEND_URL, callback=post, repeat=True)
    http.get(CONTEXT_URL, callback=get, repeat=True)
    await hass.services.async_call("climate", "set_hvac_mode", {
        "entity_id": fan_climate.entity_id, "hvac_mode": "auto"}, blocking=True)
    assert len(sent(http)) == 1
    assert str(sent(http)[0]["parameters"][field]) == "0"
    assert len(gets) == 4
    assert device.coordinator.last_update_success
    assert device._pending_command[field] == "0"
    assert device.attributes["parameters"] == remote
    assert remote[field] == "1"
    await hass.services.async_call("climate", "set_swing_mode", {
        "entity_id": fan_climate.entity_id, "swing_mode": "both"}, blocking=True)
    assert len(sent(http)) == 1
    # Only a real later context report releases the outstanding preset reset.
    remote[field] = "0"
    await device.coordinator.async_refresh()
    assert device._pending_command == {}
    assert device.attributes["parameters"] == remote
    await hass.services.async_call("climate", "set_swing_mode", {
        "entity_id": fan_climate.entity_id, "swing_mode": "both"}, blocking=True)
    assert len(sent(http)) == 2
    assert sent(http)[1]["commandName"] == "settings"
    assert str(sent(http)[1]["parameters"][field]) == "0"
    assert device._pending_command == {}
    assert device.attributes["parameters"] == remote
    assert schema_state(device) == sources
