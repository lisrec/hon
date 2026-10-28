import logging
import asyncio
import json
import voluptuous as vol
from datetime import datetime, timedelta
from typing import Optional
from enum import IntEnum
from decimal import Decimal

from homeassistant.components.climate import (
    ClimateEntity,
)

from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    CoordinatorEntity,
)

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
    SensorEntityDescription,
)


from homeassistant.components.climate.const import (

    FAN_ON,
    FAN_OFF,
    FAN_AUTO,
    FAN_LOW,
    FAN_MEDIUM,
    FAN_HIGH,
    FAN_TOP,
    FAN_MIDDLE,
    FAN_FOCUS,
    FAN_DIFFUSE,
    SWING_ON,
    SWING_OFF,
    SWING_BOTH,
    SWING_VERTICAL,
    SWING_HORIZONTAL,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_TEMPERATURE,
    PRECISION_TENTHS,
    PRECISION_WHOLE,
    STATE_OFF,
    STATE_ON,
    UnitOfTemperature,
)
from homeassistant.core import callback
from homeassistant.helpers.dispatcher   import async_dispatcher_connect
from homeassistant.helpers.event        import async_call_later
from homeassistant.helpers              import config_validation as cv, entity_platform
from .const import(
    DOMAIN, 
    CLIMATE_FAN_MODE,
    CLIMATE_HVAC_MODE,
    ClimateSwingHorizontal,
    ClimateSwingVertical,
    ClimateEcoPilotMode)

from .parameter import ( HonParameterRange, HonParameterEnum )


_LOGGER = logging.getLogger(__name__)
#SCAN_INTERVAL = timedelta(seconds=15)


async def async_setup_entry(hass, entry: ConfigEntry, async_add_entities) -> None:

    hon = hass.data[DOMAIN][entry.unique_id]

    appliances = []
    for appliance in hon.appliances:
        if appliance['applianceTypeId'] == 11:
            coordinator = await hon.async_get_coordinator(appliance)
            await coordinator.async_config_entry_first_refresh()
            appliances.append(HonClimateEntity(hass, coordinator, entry, appliance))

    async_add_entities(appliances)

    platform = entity_platform.async_get_current_platform()

    platform.async_register_entity_service(
        "climate_set_sleep_mode",
        {
            vol.Required('sleep_mode'): cv.boolean,
        },
        "async_set_sleep_mode",
    )

    platform.async_register_entity_service(
        "climate_set_screen_display",
        {
            vol.Required('screen_display'): cv.boolean,
        },
        "async_set_screen_display",
    )

    platform.async_register_entity_service(
        "climate_set_echo_mode",
        {
            vol.Required('echo_mode'): cv.boolean,
        },
        "async_set_echo_mode",
    )

    platform.async_register_entity_service(
        "climate_set_rapid_mode",
        {
            vol.Required('rapid_mode'): cv.boolean,
        },
        "async_set_rapid_mode",
    )
    
    platform.async_register_entity_service(
        "climate_set_silent_mode",
        {
            vol.Required('silent_mode'): cv.boolean,
        },
        "async_set_silent_mode",
    )

    platform.async_register_entity_service(
        "climate_set_wind_direction_vertical",
        {
            vol.Required('value'): cv.positive_int,
        },
        "async_set_wind_direction_vertical",
    )

    platform.async_register_entity_service(
        "climate_set_wind_direction_horizontal",
        {
            vol.Required('value'): cv.positive_int,
        },
        "async_set_wind_direction_horizontal",
    )

    platform.async_register_entity_service(
        "climate_set_eco_pilot_mode",
        {
            vol.Required('value'): cv.positive_int,
        },
        "async_set_eco_pilot_mode",
    )


# function to return key for any value
def get_key(dictionary,val,default):
    for key, value in dictionary.items():
        if val == value:
            return key
    _LOGGER.warning(f"Value {value} is not in dictionary {dictionary}")
    return default



class HonClimateEntity(CoordinatorEntity, ClimateEntity):
    def __init__(self,hass, coordinator, entry, appliance) -> None:
        super().__init__(coordinator)
        self._coordinator   = coordinator
        self._hon           = hass.data[DOMAIN][entry.unique_id]
        self._hass          = hass
        self._brand         = appliance['brand']
        self._mac           = appliance['macAddress']
        self._name          = appliance.get('nickName', appliance.get('modelName', 'Climate'))
        self._connectivity  = appliance['connectivity']
        self._model         = appliance['modelName']
        self._series        = appliance.get('series', '')
        self._modelId       = appliance['applianceModelId']
        self._type_name     = appliance['applianceTypeName']
        self._serialNumber  = appliance['serialNumber']
        self._fwVersion     = appliance['fwVersion']
        self._unique_id     = f"{self._mac}"
        self._available     = True
        self._watcher       = None
        self._device        = coordinator.device

        #Not working for Farenheit
        self._attr_temperature_unit         = UnitOfTemperature.CELSIUS # 'tempUnit': '0'

        self._enable_turn_on_off_backwards_compatibility = False
        self._attr_fan_modes            = [] #[FAN_OFF, FAN_LOW, FAN_MEDIUM, FAN_HIGH, FAN_AUTO]
        self._attr_hvac_modes           = [HVACMode.HEAT, HVACMode.COOL, HVACMode.AUTO, HVACMode.FAN_ONLY, HVACMode.DRY, HVACMode.OFF]
        self._attr_swing_modes          = [SWING_OFF, SWING_BOTH, SWING_VERTICAL, SWING_HORIZONTAL]
        self._attr_supported_features   = ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.FAN_MODE | ClimateEntityFeature.SWING_MODE | ClimateEntityFeature.TURN_OFF | ClimateEntityFeature.TURN_ON
        
        ''' hon specific values '''
        parameters = self._device.commands['settings'].parameters
        #_LOGGER.warning(parameters)

        # Set Min / Max temperatures
        temp_range = parameters.get('tempSel')

        self._attr_target_temperature_step  = PRECISION_WHOLE
        if isinstance(temp_range.step, float):
            self._attr_target_temperature_step = temp_range.step
    
        if isinstance(temp_range, HonParameterRange):
            self._attr_min_temp  = temp_range.min
            self._attr_max_temp  = temp_range.max
        
        # Set Fan mode
        self._hon_fan_modes = parameters.get('windSpeed').values
        for fan_mode in self._hon_fan_modes:
            self._attr_fan_modes.append(get_key(CLIMATE_FAN_MODE, fan_mode, FAN_OFF))

        self._handle_coordinator_update(False)


    async def async_set_sleep_mode(self, sleep_mode=False):

        parameters = {"silentSleepStatus": "1" if sleep_mode else "0"}
        await self._device.settings_command(parameters).send()

    async def async_set_rapid_mode(self, rapid_mode=False):

        parameters = {"rapidMode": "1" if rapid_mode else "0"}
        await self._device.settings_command(parameters).send()

    async def async_set_silent_mode(self, silent_mode=False):

        parameters = {"muteStatus": "1" if silent_mode else "0"}
        await self._device.settings_command(parameters).send()

    async def async_set_screen_display(self, screen_display=True):
        parameters = {"screenDisplayStatus": "1" if screen_display else "0"}
        await self._device.settings_command(parameters).send()

    async def async_set_echo_mode(self, echo_mode=False):

        parameters = {"echoStatus": "0" if echo_mode else "1"}
        await self._device.settings_command(parameters).send()

    async def async_set_wind_direction_horizontal(self, value: int):

        parameters = {'windDirectionHorizontal': str(value)}
        await self._device.settings_command(parameters).send()
        
    async def async_set_wind_direction_vertical(self, value: int):

        parameters = {'windDirectionVertical': str(value)}
        await self._device.settings_command(parameters).send()

    async def async_set_eco_pilot_mode(self, value: int):

        parameters = {"humanSensingStatus": value}
        await self._device.settings_command(parameters).send()

    def start_watcher(self, timedelta=timedelta(seconds=8)):
        if self._watcher is not None:
            self._watcher()
        self._watcher = async_call_later(self._hass, timedelta, self.async_update_after_state_change)
        self.async_write_ha_state()

    async def async_update_after_state_change(self, now: Optional[datetime] = None) -> None:
        self._watcher = None

    @callback
    def _handle_coordinator_update(self, update = True) -> None:

        # Watcher is running, update is not allowed because the data may not be yet accurate
        if self._watcher != None:
            return


        from math import isfinite
        def temperature(key):
            value = self._device.get(key)
            try:
                result = float(value) if not isinstance(value, bool) else None
                return result if result is not None and isfinite(result) else None
            except (ValueError, TypeError):
                return None

        self._attr_target_temperature = temperature('tempSel')
        self._attr_current_temperature = temperature('tempIndoor')
        self._attr_fan_mode = get_key(CLIMATE_FAN_MODE, self._device.get('windSpeed'), None)
        on_off = self._device.get('onOffStatus')
        self._attr_hvac_mode = None
        if on_off == "0":
            self._attr_hvac_mode = HVACMode.OFF
        elif on_off == "1":
            self._attr_hvac_mode = get_key(CLIMATE_HVAC_MODE, self._device.get('machMode'), None)
        self._available = (self._attr_target_temperature is not None
                           and self._attr_current_temperature is not None
                           and self._attr_hvac_mode is not None)


        self.update_swing_mode(self._device.get('windDirectionHorizontal'), self._device.get('windDirectionVertical'))

        self._sleep_mode    = self._device.get('silentSleepStatus') == "1"
        self._echo_mode     = self._device.get('echoStatus') == "0"
        self._screen_display= self._device.get('screenDisplayStatus') == "1"
        self._rapid_mode    = self._device.get('rapidMode') == "1"
        self._silent_mode   = self._device.get('muteStatus') == "1"
        self._wind_direction_horizontal = self._device.get('windDirectionHorizontal')
        self._wind_direction_vertical   = self._device.get('windDirectionVertical')
        self._eco_pilot_mode            = self._device.get('humanSensingStatus')

        if update: self.async_write_ha_state()


    def update_swing_mode(self, swing_horizontal, swing_vertical):
        self._attr_swing_mode = SWING_OFF
        if swing_horizontal == ClimateSwingHorizontal.AUTO and swing_vertical == ClimateSwingVertical.AUTO :
            self._attr_swing_mode = SWING_BOTH
        elif swing_horizontal == ClimateSwingHorizontal.AUTO:
            self._attr_swing_mode = SWING_HORIZONTAL
        elif swing_vertical == ClimateSwingVertical.AUTO:
            self._attr_swing_mode = SWING_VERTICAL

    @property
    def unique_id(self) -> str:
        return self._unique_id

    @property
    def name(self) -> str:
        return self._name

    @property
    def available(self):
        """Return True if entity is available."""
        return super().available and self._available

    @property
    def device_info(self):
        return {
            "identifiers": {
                # Serial numbers are unique identifiers within a specific domain
                (DOMAIN, self._mac, self._type_name)
            },
            "name": self._name,
            "manufacturer": self._brand,
            "model": self._model,
            "sw_version": self._fwVersion
        }

    @property
    def state_attributes(self):
        """Return the climate state attributes."""
        attr = super().state_attributes
        attr["sleep_mode"]      = self._sleep_mode
        attr["echo_mode"]       = self._echo_mode
        attr["rapid_mode"]      = self._rapid_mode
        attr["silent_mode"]     = self._silent_mode
        attr["screen_display"]  = self._screen_display
        attr["wind_direction_horizontal"]   = self._wind_direction_horizontal
        attr["wind_direction_vertical"]     = self._wind_direction_vertical
        attr["eco_pilot_mode"]              = self._eco_pilot_mode
        return attr

    def _mode_command(self, hvac_mode, parameters=None):
        if hvac_mode == HVACMode.OFF:
            if parameters:
                raise ValueError("OFF cannot be combined with a temperature")
            return self._device.stop_command({'onOffStatus': '0'})
        programs = {
            HVACMode.COOL: 'iot_cool', HVACMode.HEAT: 'iot_heat',
            HVACMode.DRY: 'iot_dry', HVACMode.AUTO: 'iot_auto',
            HVACMode.FAN_ONLY: 'iot_fan',
        }
        if hvac_mode not in programs:
            raise ValueError("Unsupported HVAC mode")
        parameters = dict(parameters or {})
        if (hvac_mode == HVACMode.FAN_ONLY and 'windSpeed' not in parameters
            and self._device.get('windSpeed') == CLIMATE_FAN_MODE[FAN_AUTO]):
            # AUTO may be valid in SETTINGS but unavailable in FAN. Only this
            # implicit climate transition uses the selected program's default.
            start = self._device.commands.get('startProgram')
            fan = start.get_programs().get('iot_fan') if start else None
            speed = fan.parameters.get('windSpeed') if fan else None
            if (not isinstance(speed, HonParameterEnum)
                or type(speed._values) is not list or not speed._values
                or any(type(value) not in (str, int) or str(value) == ""
                       for value in speed._values)):
                raise ValueError("No valid FAN windSpeed enum schema")
            if CLIMATE_FAN_MODE[FAN_AUTO] not in speed.values:
                parameters['windSpeed'] = speed.default
        return self._device.start_command(programs[hvac_mode], {
            **parameters, 'machMode': CLIMATE_HVAC_MODE[hvac_mode], 'onOffStatus': '1'})

    async def async_set_temperature(self, **kwargs):
        """Validate mode and target together, then send one isolated command."""
        parameters = {}
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is not None:
            parameters['tempSel'] = temperature
        if (mode := kwargs.get('hvac_mode')) is not None:
            command = self._mode_command(mode, parameters)
        elif parameters:
            power = self._device.get('onOffStatus')
            mode = self._device.get('machMode')
            if power not in ('0', '1') or mode not in CLIMATE_HVAC_MODE.values():
                raise ValueError("No valid reported climate basis")
            preserved = {'onOffStatus': power, 'machMode': mode}
            schema = self._device.commands['settings'].parameters
            command = self._device.settings_command({
                **parameters, **{key: value for key, value in preserved.items() if key in schema}})
        else:
            raise ValueError("No climate intent supplied")
        await command.send()


    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self._mode_command(hvac_mode).send()


    async def async_turn_off(self) -> None:
        await self._device.stop_command({'onOffStatus': '0'}).send()

    async def async_turn_on(self) -> None:
        await self._device.start_command('iot_simple_start', {'onOffStatus': '1'}).send()


    async def async_set_fan_mode(self, fan_mode: str):
        if fan_mode not in self._attr_fan_modes:
            raise ValueError("Unsupported fan mode")
        await self._device.settings_command({'windSpeed': CLIMATE_FAN_MODE[fan_mode]}).send()


    async def async_set_swing_mode(self, swing_mode: str):
        if swing_mode not in self._attr_swing_modes:
            raise ValueError("Unsupported swing mode")
        if swing_mode == SWING_BOTH:
            parameters = {'windDirectionHorizontal': ClimateSwingHorizontal.AUTO, 'windDirectionVertical': ClimateSwingVertical.AUTO}

        elif swing_mode == SWING_HORIZONTAL and self._device.get('windDirectionVertical') == ClimateSwingVertical.AUTO:
            parameters = {'windDirectionHorizontal': ClimateSwingHorizontal.AUTO, 'windDirectionVertical': ClimateSwingVertical.MIDDLE}

        elif swing_mode == SWING_HORIZONTAL:
            parameters = {'windDirectionHorizontal': ClimateSwingHorizontal.AUTO}

        elif swing_mode == SWING_VERTICAL and self._device.get('windDirectionHorizontal') == ClimateSwingHorizontal.AUTO:
            parameters = {'windDirectionHorizontal': ClimateSwingHorizontal.MIDDLE, 'windDirectionVertical': ClimateSwingVertical.AUTO}

        elif swing_mode == SWING_VERTICAL:
            parameters = {'windDirectionVertical': ClimateSwingVertical.AUTO}

        else: #off
            parameters = {}
            if self._device.get('windDirectionHorizontal') == ClimateSwingHorizontal.AUTO:
                parameters['windDirectionHorizontal'] =  ClimateSwingHorizontal.MIDDLE
            if self._device.get('windDirectionVertical') == ClimateSwingVertical.AUTO:
                parameters['windDirectionVertical'] =  ClimateSwingVertical.MIDDLE

        await self._device.settings_command(parameters).send()


    async def async_will_remove_from_hass(self):
        """When entity will be removed from hass."""
        if self._watcher is not None:
            self._watcher()
            self._watcher = None

