"""Real catalog fixed preset intent must not be silently reverted by SETTINGS."""
from copy import deepcopy
import pytest
from aioresponses import CallbackResult
from test_ac_fan_transition import MODELS, MODEL_IDS, fan_climate, schema_state
from test_command_readback import CONTEXT_URL, snapshot
from test_quiet_command_transport import SEND_URL, sent

@pytest.mark.parametrize('fan_climate',MODELS,ids=MODEL_IDS,indirect=True)
@pytest.mark.parametrize('field',['rapidMode','muteStatus'])
async def test_auto_fixed_preset_intent_fences_stale_preset_report(
    hass,device,connection,http,fan_climate,field
):
    device.attributes['parameters'][field]='1'
    device.coordinator.async_update_listeners()
    before=deepcopy(device.attributes['parameters'])
    sources=schema_state(device)
    remote=deepcopy(before)
    def post(url,**kw):
        # Cloud reports core mode/target/fan fields immediately, but the fixed
        # preset reset is delayed. This is not fabricated production evidence.
        remote.update({k:str(v) for k,v in kw['json']['parameters'].items()
                       if k in {'onOffStatus','machMode','tempSel','windSpeed',
                                'windDirectionHorizontal','windDirectionVertical'}})
        return CallbackResult(payload={'payload':{'resultCode':'0'}})
    http.post(SEND_URL,callback=post,repeat=True)
    http.get(CONTEXT_URL,callback=lambda url,**kw: CallbackResult(payload=snapshot(remote)),repeat=True)
    await hass.services.async_call('climate','set_hvac_mode',{
        'entity_id':fan_climate.entity_id,'hvac_mode':'auto'},blocking=True)
    assert str(sent(http)[0]['parameters'][field])=='0'
    assert device.attributes['parameters'][field]=='1'
    await hass.services.async_call('climate','set_swing_mode',{
        'entity_id':fan_climate.entity_id,'swing_mode':'both'},blocking=True)
    assert schema_state(device)==sources
    assert len(sent(http))==1, (
        f'Accepted AUTO reset {field}=0 was followed by SETTINGS restoring '
        f'{field}={sent(http)[1]["parameters"][field]} from stale report')
