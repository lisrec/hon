import asyncio
import logging
from copy import copy, deepcopy

from homeassistant.helpers.update_coordinator import (DataUpdateCoordinator,CoordinatorEntity, UpdateFailed)

from .const import DOMAIN, APPLIANCE_DEFAULT_NAME
from .command import HonCommand
from .parameter import HonParameterFixed, HonParameterEnum

_LOGGER = logging.getLogger(__name__)

class HonDevice(CoordinatorEntity):
    def __init__(self, hon, coordinator, appliance) -> None:
        super().__init__(coordinator)

        self._hon           = hon
        self._coordinator   = coordinator
        self._appliance     = appliance
        self._brand         = appliance["brand"]
        self._type_name     = appliance["applianceTypeName"]
        self._type_id       = appliance["applianceTypeId"]
        self._name          = appliance.get("nickName", APPLIANCE_DEFAULT_NAME.get(str(self._type_id), "Device ID: " + str(self._type_id)))
        self._mac           = appliance["macAddress"]
        self._model         = appliance["modelName"]
        self._series        = appliance.get("series", "")
        self._model_id      = appliance["applianceModelId"]
        self._serial_number = appliance["serialNumber"]
        self._fw_version    = appliance["fwVersion"]

        self._commands              = {}
        self._appliance_model       = {}
        self._attributes            = {}
        self._statistics            = {}
        # Command authorization is separate from the connector's POST lock:
        # readback must be free to take that lock while this transaction settles.
        self._command_transaction = asyncio.Lock()
        self._command_generation = 0
        self._pending_command = {}


    def __getitem__(self, item):
        if "." in item:
            result = self.data
            for key in item.split("."):
                if all([k in "0123456789" for k in key]) and type(result) is list:
                    result = result[int(key)]
                else:
                    result = result[key]
            return result
        else:
            if item in self.data:
                return self.data[item]
            if item in self.attributes["parameters"]:
                return self.attributes["parameters"].get(item)
            return self.appliance[item]

    def set(self, item, value):
        if item in self.data:
            self.data[item] = value
        elif item in self.attributes["parameters"]:
            self.attributes["parameters"][item] = value
        else: 
            self.appliance[item] = value
    
    def get(self, item, default=None):
        try:
            return self[item]
        except (KeyError, IndexError):
            return default
    
    def getInt(self, item):
        return int(self.get(item,0))

    def getFloat(self, item):
        return float(self.get(item,0))

    def has(self, item, default=None):
        return self.get(item) != None

    def getProgramName(self):
        try:
            # Önce activity.attributes içinde ara
            activity = self._attributes.get("activity", {})
            if activity:
                program_name = activity.get("attributes", {}).get("programName")
                if program_name:
                    _LOGGER.debug(f"[{self._name}] Found program name in activity.attributes: {program_name}")
                    name = program_name.lower()
                    parts = name.split('.')
                    if len(parts) == 3:
                        return parts[2]
                    return name
            
            # Sonra direkt attributes içinde ara
            program_name = self._attributes.get("programName")
            if program_name:
                _LOGGER.debug(f"[{self._name}] Found program name in attributes: {program_name}")
                name = program_name.lower()
                parts = name.split('.')
                if len(parts) == 3:
                    return parts[2]
                return name
            
            # Sonra commandHistory içinde ara
            command_history = self._attributes.get("commandHistory", {})
            if command_history:
                command = command_history.get("command", {})
                program_name = command.get("programName")
                if program_name:
                    _LOGGER.debug(f"[{self._name}] Found program name in commandHistory: {program_name}")
                    name = program_name.lower()
                    parts = name.split('.')
                    if len(parts) == 3:
                        return parts[2]
                    return name
            
            _LOGGER.debug(f"[{self._name}] Program name not found in any location")
                
        except Exception as e:
            _LOGGER.warning(f"[{self._name}] Failed to get program name: {e}")
        
        return None

    """    
    async def load_context(self):
        data = await self._hon.async_get_context(self)
        #_LOGGER.warning(data)
        self._attributes = data
        for name, values in self._attributes.pop("shadow", {'NA': 0}).get("parameters").items():
            self._attributes.setdefault("parameters", {})[name] = values["parNewVal"]
    """
    async def load_context(self):

        data = await self._hon.async_get_context(self)
        shadow = data.get("shadow") if isinstance(data, dict) else None
        parameters = shadow.get("parameters") if isinstance(shadow, dict) else None
        if (not isinstance(parameters, dict) or not parameters
            or any(not isinstance(value, dict) or "parNewVal" not in value for value in parameters.values())):
            raise UpdateFailed("hOn returned an invalid device snapshot")
        snapshot = deepcopy(data)
        snapshot.pop("shadow")
        snapshot["parameters"] = {key: value["parNewVal"] for key, value in parameters.items()}
        self._attributes = snapshot
        # Even a confirming GET cannot rebase a draft already built from an
        # older snapshot while another transaction was in flight.
        self._command_generation += 1
        if self._pending_command and HonCommand._parameters_match(snapshot["parameters"], self._pending_command):
            # Confirmation comes only from a successfully parsed actual GET.
            # Later remote changes must not be fenced by historical intent.
            self._pending_command = {}
            
    @property
    def data(self):
        return {"attributes": self.attributes, "appliance": self.appliance, "statistics": self.statistics, **self.parameters}

    @property
    def appliance_type(self):
        return self._appliance.get("applianceTypeName")

    @property
    def mac_address(self):
        return self._appliance.get("macAddress")

    @property
    def model_name(self):
        return self._appliance.get("modelName")

    @property
    def name(self):
        return self._name

    @property
    def commands_options(self):
        return self._appliance_model.get("options")

    @property
    def commands(self):
        return self._commands
    
    @property
    def attributes(self):
        return self._attributes

    @property
    def statistics(self):
        return self._statistics

    @property
    def appliance(self):
        return self._appliance

    @property
    def settings(self):
        result = {}
        for name, command in self._commands.items():
            for key, setting in command.settings.items():
                result[f"{name}.{key}"] = setting
        return result

    @property
    def parameters(self):
        result = {}
        for name, command in self._commands.items():
            for key, parameter in command.parameters.items():
                result.setdefault(name, {})[key] = parameter.value
        return result
        
    """ 
    def update_command(self, command, parameters):
        for key in command.parameters.keys():
            if( key in parameters 
                and command.parameters.get(key).value != parameters.get(key) 
                and not isinstance(command.parameters.get(key), HonParameterFixed)):

                if( isinstance(command.parameters.get(key), HonParameterEnum) and parameters.get(key) not in command.parameters.get(key).values): 
                    _LOGGER.warning(f"Unable to update parameter [{key}] with value [{parameters.get(key)}] because not in range {command.parameters.get(key).values}. Use default instead.")
                else:
                    command.parameters.get(key).value = parameters.get(key) """

    def seed_command(self, command, parameters, requested):
        for key, parameter in command.parameters.items():
            if key in requested or isinstance(parameter, HonParameterFixed):
                continue
            if not self.coordinator.last_update_success:
                raise ValueError("No current reported basis after failed update")
            if key not in parameters:
                raise ValueError(f"No reported basis for parameter: {key}")
            parameter.value = parameters[key]

    def update_command(self, command, parameters):
        """Validate every explicit field; never silently substitute defaults."""
        if not isinstance(parameters, dict):
            raise ValueError("Parameters must be a dictionary")
        for key, value in parameters.items():
            if key not in command.parameters:
                raise ValueError(f"Unsupported parameter: {key}")
            command.parameters[key].value = value

    def _draft_command(self, name, parameters=None, program=None):

        if name not in self._commands:
            raise ValueError(f"Unsupported command: {name}")
        parameters = {} if parameters is None else parameters
        if not isinstance(parameters, dict):
            raise ValueError("Parameters must be a dictionary")
        parameters = dict(parameters)
        requested_program = parameters.pop("program", program)
        source = self._commands[name]
        if requested_program is not None:
            if program is not None and requested_program != program:
                raise ValueError("Conflicting program intent")
            if requested_program not in source.get_programs():
                raise ValueError("Unsupported program")
            source = source.get_programs()[requested_program]
        command = copy(source)
        command._basis_generation = self._command_generation
        # Program selectors are local schema controls, never device parameters.
        command._parameters = {key: copy(value) for key, value in source.parameters.items() if key != "program"}
        power = command.parameters.get("onOffStatus")
        if (name == "settings" and self.appliance_type == "AC"
            and isinstance(power, HonParameterFixed)
            and type(power._value) in (str, int) and str(power._value) in ("0", "1")):
            reported = self.attributes.get("parameters", {}).get("onOffStatus")
            if (not self.coordinator.last_update_success
                or type(reported) not in (str, int) or str(reported) not in ("0", "1")):
                raise ValueError("No current reported binary power basis")
            # SETTINGS preserves power; rebase only this copied fixed field.
            # The fixed setter still rejects explicit conflicting power intent.
            power._value = str(reported) if isinstance(power.value, str) else int(reported)
        self.seed_command(command, self.attributes.get("parameters", {}), parameters)
        self.update_command(command, parameters)
        return command

    def settings_command(self, parameters=None):
        return self._draft_command("settings", parameters)

    def start_command(self, program=None, parameters=None):
        return self._draft_command("startProgram", parameters, program)

    def get_setting(self, setting_key):
        command_name, parameter_key = setting_key.split(".", 1)
        command = self._commands.get(command_name)
        if command is None:
            return None
        return command.settings.get(parameter_key)

    def has_current_setting(self, setting_key):
        command_name, parameter_key = setting_key.split(".", 1)
        command = self._commands.get(command_name)
        if command is None:
            return False
        return parameter_key in command.parameters

    def stop_command(self, parameters=None):
        return self._draft_command("stopProgram", parameters)

    async def load_commands(self):
        commands = await self._hon.load_commands(self._appliance)
    
        try:
            self._appliance_model = commands.pop("applianceModel")
        except:
            _LOGGER.error(f"Unable to load device commands. Please try to restart. Current value: [{commands}]")
            return

        for item in ["options", "dictionaryId"]:
            commands.pop(item)

        for command, attr in commands.items():
            if "parameters" in attr:
                self._commands[command] = HonCommand(command, attr, self._hon, self)
            if "setParameters" in attr and "parameters" in attr[list(attr)[0]]:
                self._commands[command] = HonCommand(command, attr.get("setParameters"), self._hon, self)
            elif "parameters" in attr[list(attr)[0]]:
                multi = {}
                for program, attr2 in attr.items():
                    program = program.split(".")[-1].lower()
                    cmd = HonCommand(command, attr2, self._hon, self, multi=multi, program=program)
                    multi[program] = cmd
                    self._commands[command] = cmd

    async def load_statistics(self):
        self._statistics = await self._hon.load_statistics(self)

    @property
    def device_info(self):
        return {
            "identifiers": {
                (DOMAIN, self._mac, self._type_name)
            },
            "name": self._name,
            "manufacturer": self._brand,
            "model": self._model,
            "sw_version": self._fw_version,
        }
