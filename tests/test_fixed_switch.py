"""Fixed schema parameters must not escape strict switch preflight."""
from copy import deepcopy

import pytest

from custom_components.hon.command import HonCommand
from custom_components.hon.switch import HonSwitchEntity, HonSwitchEntityDescription
from test_quiet_command_transport import SEND_URL, command_schema, sent


@pytest.mark.parametrize("key", ["screenDisplayStatus", "healthMode"])
@pytest.mark.parametrize("fixed", ["0", "1"])
@pytest.mark.parametrize("invert", [False, True])
async def test_fixed_switch_conflict_rejected_before_post(
    hass, entry, device, connection, http, key, fixed, invert
):
    schema = command_schema()
    schema["parameters"][key] = {"typology": "fixed", "fixedValue": fixed}
    device.commands["settings"] = HonCommand("settings", schema, connection, device)
    device.attributes["parameters"][key] = fixed
    entity = HonSwitchEntity(
        hass, device.coordinator, entry, device.appliance,
        HonSwitchEntityDescription(key=key, name=key), invert=invert,
    )
    device.coordinator.last_update_success = True
    before = deepcopy(device.attributes)
    http.post(SEND_URL, payload={"payload": {"resultCode": "0"}}, repeat=True)
    # Choose the logical direction that conflicts with the fixed wire value.
    turn_on = (fixed == "0") != invert
    with pytest.raises(ValueError):
        await entity._async_set_switch(turn_on)
    assert not http.requests
    assert device.attributes == before
    # An already satisfied fixed value remains a valid intent.
    await entity._async_set_switch(not turn_on)
    assert len(sent(http)) == 1
    assert sent(http)[0]["parameters"][key] == fixed
    assert device.attributes == before
