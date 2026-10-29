#All credits to https://github.com/Andre0512/pyhOn
import asyncio

from homeassistant.helpers.update_coordinator import UpdateFailed

from .parameter import HonParameterFixed, HonParameterEnum, HonParameterRange, HonParameterProgram, _decimal

import logging
_LOGGER = logging.getLogger(__name__)

# Total post-acceptance readback budget, including GETs and retry delays.
_COMMAND_TIMEOUT = 4

class HonCommand:
    def __init__(self, name, attributes, connector, device, multi=None, program=""):
        self._connector = connector
        self._device = device
        self._basis_generation = device._command_generation
        self._name = name
        self._multi = multi or {}
        self._program = program
        self._description = attributes.get("description", "")
        self._parameters = self._create_parameters(attributes.get("parameters", {}))
        self._ancillary_parameters = self._create_parameters(attributes.get("ancillaryParameters", {}))

    def __repr__(self):
        return f"{self._name} command"

    def _create_parameters(self, parameters):
        result = {}
        for parameter, attributes in parameters.items():
            match attributes.get("typology"):
                case "range":
                    result[parameter] = HonParameterRange(parameter, attributes)
                case "enum":
                    result[parameter] = HonParameterEnum(parameter, attributes)
                case "fixed":
                    result[parameter] = HonParameterFixed(parameter, attributes)
        if self._multi:
            result["program"] = HonParameterProgram("program", self)
        return result

    @property
    def parameters(self):
        return self._parameters

    @property
    def ancillary_parameters(self):
        return {key: parameter.value for key, parameter in self._ancillary_parameters.items()}

    async def send(self):
        guarded = (self._device.appliance_type == "AC"
                   and self._name in ("startProgram", "stopProgram", "settings"))
        if not guarded:
            return await self._send()
        async with self._device._command_transaction:
            # OFF remains possible without a usable reported settings basis.
            power = self._parameters.get("onOffStatus")
            neutral_off = (self._name == "stopProgram" and power is not None
                           and type(power.value) in (str, int) and str(power.value) == "0"
                           and not any(key not in ("onOffStatus", "echoStatus")
                                       and (key in ("machMode", "tempSel", "windSpeed",
                                                    "windDirectionHorizontal", "windDirectionVertical")
                                            or not isinstance(parameter, HonParameterFixed))
                                       for key, parameter in self._parameters.items()))
            pending = self._device._pending_command
            if not neutral_off and (
                self._basis_generation != self._device._command_generation
                or (pending and (not self._device.coordinator.last_update_success
                                 or not self._reported_matches(pending)))
            ):
                _LOGGER.warning("hOn command withheld: reported basis has unresolved or superseded intent")
                return False
            return await self._send()

    async def _send(self):
        parameters = {name: parameter.value for name, parameter in self._parameters.items()}
        accepted = await self._connector.send_command(self._device, self._name, parameters, self.ancillary_parameters)
        if accepted:
            # Read actual context after the POST lock is released. Bypass the
            # request debouncer so consecutive commands each get fresh telemetry.
            # Device control fields can be fixed in one program and mutable in
            # SETTINGS. Fence those reports, not arbitrary protocol markers.
            fields = {"onOffStatus", "machMode", "tempSel", "windSpeed",
                      "windDirectionHorizontal", "windDirectionVertical",
                      "rapidMode", "muteStatus"}
            readback_fields = {"onOffStatus"} if self._name == "stopProgram" else fields
            expected = {key: value for key, value in parameters.items() if key in readback_fields}
            bounded = (self._device.appliance_type == "AC"
                       and self._name in ("startProgram", "stopProgram", "settings")
                       and bool(expected))
            coordinator = self._device.coordinator
            if bounded:
                # Accepted intent is a fence, never reported telemetry or a seed.
                # Retain it through stale/failed/cancelled GETs; only an actual
                # matching report can authorize the next ordinary transaction.
                self._device._command_generation += 1
                # A power-only OFF supersedes power intent, not unconfirmed fan
                # or target intent. This combines fences only, never draft data.
                previous = self._device._pending_command if self._name == "stopProgram" else {}
                intent = {key: value for key, value in parameters.items() if key in fields}
                self._device._pending_command = {**previous, **expected, **intent}
            if not bounded:
                await coordinator.async_refresh()
                return accepted
            try:
                async with asyncio.timeout(_COMMAND_TIMEOUT):
                    for attempt in range(4):
                        if attempt:
                            await asyncio.sleep(1)
                        await coordinator.async_refresh()
                        if (not coordinator.last_update_success
                            or self._reported_matches(expected)):
                            break
            except TimeoutError:
                # HA marks a cancelled refresh unsuccessful but re-raises before
                # notifying entities. Publish the timeout even in that case.
                was_successful = coordinator.last_update_success
                coordinator.async_set_update_error(UpdateFailed("hOn command readback timed out"))
                if not was_successful:
                    coordinator.async_update_listeners()
        return accepted

    def _reported_matches(self, expected):
        return self._parameters_match(self._device.attributes.get("parameters", {}), expected)

    @staticmethod
    def _parameters_match(reported, expected):
        for key, value in expected.items():
            actual = reported.get(key)
            if key == "tempSel":
                try:
                    if _decimal(actual) != _decimal(value):
                        return False
                except ValueError:
                    return False
            elif (type(actual) not in (str, int) or type(value) not in (str, int)
                  or not str(value) or str(actual) != str(value)):
                return False
        return True

    def get_programs(self):
        return self._multi

    def set_program(self, program):
        self._multi[program]._multi = self._multi
        self._device.commands[self._name] = self._multi[program]
    
    def _get_settings_keys(self, command=None):
        command = command or self
        keys = []
        for key, parameter in command._parameters.items():
            if isinstance(parameter, HonParameterFixed):
                continue
            if key not in keys:
                keys.append(key)
        return keys

    @property
    def setting_keys(self):
        if not self._multi:
            return self._get_settings_keys()
        result = [key for cmd in self._multi.values() for key in self._get_settings_keys(cmd)]
        return list(set(result + ["program"]))

    @property
    def settings(self):
        """Parameters with typology enum and range"""
        if not self._multi:
            return {
                key: parameter
                for key, parameter in self._parameters.items()
                if not isinstance(parameter, HonParameterFixed)
            }

        result = {}
        for key in self.setting_keys:
            parameter = self._parameters.get(key)
            if parameter is None:
                for command in self._multi.values():
                    parameter = command.parameters.get(key)
                    if parameter is not None:
                        break
            if parameter is not None:
                result[key] = parameter
        return result

    def dump(self):
        text = ""
        example = "{"
        for key, parameter in self._parameters.items():
            if isinstance(parameter, HonParameterFixed) or key == "program":
                continue
            text += f"""{parameter.dump()}
"""
            example += f"\'{key}\':{parameter.default},"
        example = example[:-1] + "}"
        return text, example
