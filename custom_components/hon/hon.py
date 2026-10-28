import asyncio
import logging
import voluptuous as vol
import aiohttp
import asyncio
import secrets
import hashlib
import base64
import json
import re
import ast
import time
import urllib.parse
from urllib.parse import quote
from datetime import datetime, timezone, timedelta
from homeassistant.helpers.update_coordinator import UpdateFailed

_LOGGER = logging.getLogger(__name__)

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.exceptions import HomeAssistantError, ConfigEntryAuthFailed

from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import device_registry as dr


from .const import (
    DOMAIN,
    CONF_QUIET_AC_COMMANDS,
    DEFAULT_QUIET_AC_COMMANDS,
    CONF_ID_TOKEN,
    CONF_COGNITO_TOKEN,
    CONF_REFRESH_TOKEN,
    API_URL,
    APP_VERSION,
    OS_VERSION,
    OS,
    DEVICE_MODEL,
)

# CIAM access tokens expire after ~15 minutes, so refresh well before that.
SESSION_TIMEOUT     = 600 # seconds
# Bound retry bursts to the normal coordinator polling interval.
AUTH_RETRY_COOLDOWN = 60 # seconds

from .base import HonBaseCoordinator



def _prepare_command_parameters(appliance_type, parameters, *, quiet_ac_commands):
    """Apply the local quiet policy without changing cloud or builder state."""
    prepared = dict(parameters)
    if quiet_ac_commands and appliance_type == "AC":
        prepared["echoStatus"] = "1"
    return prepared


class HonConnection:
    def __init__(self, hass, entry, email = None, password = None) -> None:
        self._hass = hass
        self._entry = entry
        self._coordinator_dict  = {}
        self._mobile_id = secrets.token_hex(8)

        # Only used during registration (Login/password check)
        if( email != None ) and ( password != None ):
            self._email = email
            self._password = password
        else:
            self._email = entry.data[CONF_EMAIL]
            self._password = entry.data[CONF_PASSWORD]
            self._id_token = entry.data.get(CONF_ID_TOKEN, "")
            self._refresh_token = entry.data.get(CONF_REFRESH_TOKEN, "")
            self._cognitoToken = entry.data.get(CONF_COGNITO_TOKEN, "")

        self._start_time    = time.time()
        self._session_lock = asyncio.Lock()
        self._command_locks = {}
        self._auth_generation = 0
        self._auth_failed = False
        self._login_retry_after = 0

        self._header = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/102.0.0.0 Safari/537.36"
        }
        self._session = aiohttp.ClientSession(headers=self._header)
        self._appliances = []

    async def _request_json(self, method, path, *, error="hOn request failed", **kwargs):
        """Keep response bodies, credential-bearing URLs and client errors private."""
        try:
            # Never replay commands or forward credentials to a redirect target.
            async with self._session.request(
                method, f"{API_URL}{path}", allow_redirects=False, **kwargs
            ) as response:
                if response.status in (401, 403):
                    self._auth_failed = True
                    raise ConfigEntryAuthFailed("hOn authentication failed")
                if response.status != 200:
                    raise HomeAssistantError(error)
                data = await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError):
            raise HomeAssistantError(error) from None
        if not isinstance(data, dict):
            raise HomeAssistantError(error)
        return data

    async def async_close(self):
        """Stop owned coordinators before closing the owned HTTP session."""
        try:
            for coordinator in self._coordinator_dict.values():
                await coordinator.async_shutdown()
        finally:
            await self._session.close()

    @property
    def appliances(self):
        return self._appliances

    async def async_get_existing_coordinator(self, mac):
        if mac in self._coordinator_dict:
            return self._coordinator_dict[mac]
        return None

    async def async_get_coordinator(self, appliance):
        mac = appliance.get("macAddress", "")
        if mac in self._coordinator_dict:
            return self._coordinator_dict[mac]
        coordinator = HonBaseCoordinator(self._hass, self, appliance)
        self._coordinator_dict[mac] = coordinator
        return coordinator


    async def _ensure_session(self):
        """Re-authenticate when the CIAM tokens are close to expiring (~15 min TTL)."""
        if self._auth_failed:
            raise ConfigEntryAuthFailed("hOn authentication failed")
        if time.time() - self._start_time > SESSION_TIMEOUT:
            async with self._session_lock:
                if self._auth_failed:
                    raise ConfigEntryAuthFailed("hOn authentication failed")
                # Another poll/command may already have refreshed while we waited.
                if time.time() - self._start_time > SESSION_TIMEOUT:
                    await self._authorize_session()

    async def async_authorize(self):
        """Share an overlapping login without recursively acquiring the lock."""
        generation = self._auth_generation
        async with self._session_lock:
            if generation != self._auth_generation and not self._auth_failed:
                return True
            return await self._authorize_session()

    async def _authorize_session(self):
        """Stop failed logins from fanning out into stale-token operations/retries."""
        if self._auth_failed:
            raise ConfigEntryAuthFailed("hOn authentication failed")
        if time.monotonic() < self._login_retry_after:
            raise HomeAssistantError("hOn login temporarily unavailable; retry later")
        self._start_time = 0
        try:
            return await self._async_authorize()
        except ConfigEntryAuthFailed:
            raise
        except HomeAssistantError:
            self._login_retry_after = time.monotonic() + AUTH_RETRY_COOLDOWN
            raise

    async def _async_authorize(self):
        """Authenticate against the hOn CIAM endpoint and load the appliances.

        Replaces the legacy Salesforce Aura / OAuth2 login that Haier retired in
        2026-06: the app now logs in through /ciam/authorize + /ciam/token (PKCE)
        and reads appliances from /unified-api/v1/view/appliance-list. The old
        /commands/v1/appliance endpoint now returns an empty list.
        """
        # PKCE (S256) verifier + challenge
        code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        ).rstrip(b"=").decode()

        # 1) Submit credentials, receive a one-time session id
        params = {
            "username": self._email,
            "password": self._password,
            "code_challenge": code_challenge,
        }
        data = await self._request_json("GET", "/ciam/authorize", params=params)
        session_id = data.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise HomeAssistantError("hOn returned an invalid authorization response")

        # 2) Exchange the session id (+ PKCE verifier) for the tokens
        data = await self._request_json(
            "POST", "/ciam/token",
            json={"session_id": session_id, "code_verifier": code_verifier},
        )
        tokens = data.get("tokens")
        if not isinstance(tokens, dict) or any(
            not isinstance(tokens.get(key), str) or not tokens[key]
            for key in ("cognito_token", "id_token")
        ):
            raise HomeAssistantError("hOn returned an invalid token response")

        # 3) Load inventory before publishing a successfully authenticated session.
        data = await self._request_json(
            "POST", "/unified-api/v1/view/appliance-list",
            headers={"cognito-token": tokens["cognito_token"], "id-token": tokens["id_token"]},
            json={"deviceId": "homeassistant"},
        )
        try:
            appliances = data["modules"]["applianceList"]["payload"]["appliances"]
        except (KeyError, TypeError):
            raise HomeAssistantError("hOn returned an invalid appliance list") from None
        if not isinstance(appliances, list) or any(not isinstance(item, dict) for item in appliances):
            raise HomeAssistantError("hOn returned an invalid appliance list")
        self._appliances = [
            item for item in appliances if "macAddress" in item and "applianceTypeId" in item
        ]
        self._cognitoToken = tokens["cognito_token"]
        self._id_token = tokens["id_token"]
        self._refresh_token = tokens.get("refresh_token", "")

        self._start_time = time.time()
        self._auth_generation += 1
        self._auth_failed = False
        return True


    async def load_commands(self, appliance):
        await self._ensure_session()
        params = {
            "applianceType": appliance["applianceTypeId"],
            "code": appliance["code"],
            "applianceModelId": appliance["applianceModelId"],
            "firmwareId": appliance["eepromId"],
            "macAddress": appliance["macAddress"],
            "fwVersion": appliance["fwVersion"],
            "os": OS,
            "appVersion": APP_VERSION,
            "series": appliance["series"],
        }
        data = await self._request_json(
            "GET", "/commands/v1/retrieve", params=params, headers=self._headers
        )
        result = data.get("payload")
        if not isinstance(result, dict) or result.get("resultCode") != "0":
            raise HomeAssistantError("hOn returned an invalid commands response")
        result.pop("resultCode")
        return result

    async def async_get_context(self, device):

        params = {
            "macAddress": device.mac_address,
            "applianceType": device.appliance_type,
            "category": "CYCLE"
        }
        try:
            await self._ensure_session()
            data = await self._request_json(
                "GET", "/commands/v1/context", params=params, headers=self._headers
            )
        except ConfigEntryAuthFailed:
            raise
        except HomeAssistantError:
            raise UpdateFailed("hOn context request failed") from None
        payload = data.get("payload")
        if not isinstance(payload, dict):
            raise UpdateFailed("hOn returned an invalid context")
        shadow = payload.get("shadow")
        parameters = shadow.get("parameters") if isinstance(shadow, dict) else None
        # Validate the container consumed by load_context before it replaces state.
        if (
            not isinstance(parameters, dict) or not parameters
            or any(not isinstance(value, dict) or "parNewVal" not in value for value in parameters.values())
            or ("parameters" in payload and not isinstance(payload["parameters"], dict))
            or ("resultCode" in payload and payload["resultCode"] != "0")
        ):
            raise UpdateFailed("hOn returned an invalid context")
        return payload

    async def load_statistics(self, device):
        await self._ensure_session()
        params = {
            "macAddress": device.mac_address,
            "applianceType": device.appliance_type
        }
        data = await self._request_json(
            "GET", "/commands/v1/statistics", params=params, headers=self._headers
        )
        payload = data.get("payload")
        if not isinstance(payload, dict):
            raise HomeAssistantError("hOn returned an invalid statistics response")
        return payload

    @property
    def _headers(self):
        return {
            "Content-Type": "application/json",
            "cognito-token": self._cognitoToken,
            "id-token": self._id_token,
        }

    async def async_set(self, mac, typeName, parameters):
        async with self._command_locks.setdefault(mac, asyncio.Lock()):
            return await self._async_set(mac, typeName, parameters)

    async def _async_set(self, mac, typeName, parameters):

        await self._ensure_session()
        quiet_ac_commands = self._entry.options.get(
            CONF_QUIET_AC_COMMANDS, DEFAULT_QUIET_AC_COMMANDS
        )

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        command = json.loads("{}")
        command["macAddress"] = mac
        command["commandName"] = "startProgram"
        command["applianceOptions"] = json.loads("{}")
        command["programName"] = "PROGRAMS." + typeName + ".HOME_ASSISTANT"
        command["ancillaryParameters"] = json.loads(
            '{"programFamily":"[standard]", "remoteActionable": "1", "remoteVisible": "1"}'
        )
        command["applianceType"] = typeName
        command["attributes"] = json.loads(
            '{"prStr":"HOME_ASSISTANT", "channel":"googleHome", "origin": "conversationalVoice"}'
        )
        if typeName == "WM":
            command["attributes"] = json.loads(
            '{"prStr":"HOME_ASSISTANT", "channel":"googleHome", "origin": "conversationalVoice", "energyLabel": "0"}'
        )
        command["device"] = json.loads(
            '{"mobileId":"xxxxxxxxxxxxxxxxxxx", "mobileOs": "android", "osVersion": "31", "appVersion": "1.53.4", "deviceModel": "lito"}'
        )
        command["parameters"] = _prepare_command_parameters(
            typeName,
            parameters,
            quiet_ac_commands=quiet_ac_commands,
        )
        command["timestamp"] = timestamp
        command["transactionId"] = mac + "_" + command["timestamp"]
        return await self._send(command, quiet=typeName == "AC" and quiet_ac_commands)

    async def _send(self, command, *, quiet):
        """A command succeeds only with a valid cloud acknowledgement; never retry."""
        label = "quiet AC command" if quiet else "command"
        data = await self._request_json(
            "POST", "/commands/v1/send", headers=self._headers, json=command,
            error=f"hOn failed to send the {label}",
        )
        payload = data.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("resultCode"), str):
            raise HomeAssistantError(f"hOn returned an invalid response to the {label}")
        if payload["resultCode"] != "0":
            raise HomeAssistantError(f"hOn rejected the {label}")
        return True

    async def send_command(self, device, command, parameters, ancillary_parameters):
        async with self._command_locks.setdefault(device.mac_address, asyncio.Lock()):
            return await self._send_command(device, command, parameters, ancillary_parameters)

    async def _send_command(self, device, command, parameters, ancillary_parameters):

        await self._ensure_session()
        quiet_ac_commands = self._entry.options.get(
            CONF_QUIET_AC_COMMANDS, DEFAULT_QUIET_AC_COMMANDS
        )

        now = datetime.utcnow().isoformat()
        command = {
            "macAddress": device.mac_address,
            "timestamp": f"{now[:-3]}Z",
            "commandName": command,
            "transactionId": f"{device.mac_address}_{now[:-3]}Z",
            "applianceOptions": device.commands_options,
            "device": {
                "mobileId": self._mobile_id,
                "mobileOs": OS,
                "osVersion": OS_VERSION,
                "appVersion": APP_VERSION,
                "deviceModel": DEVICE_MODEL
            },
            "attributes": {
                "channel": "mobileApp",
                "origin": "standardProgram",
                "energyLabel": "0"
            },
            "ancillaryParameters": ancillary_parameters,
            "parameters": _prepare_command_parameters(
                device.appliance_type,
                parameters,
                quiet_ac_commands=quiet_ac_commands,
            ),
            "applianceType": device.appliance_type
        }
        return await self._send(
            command, quiet=device.appliance_type == "AC" and quiet_ac_commands
        )

    def get_device(self, hass, device_id):
        mac = get_hOn_mac(device_id, hass)
        if mac in self._coordinator_dict:
            return self._coordinator_dict[mac].device
        _LOGGER.error(f"Unable to find the device with ID: {device_id} and mac: {mac}")
        return None

def get_hOn_mac(device_id, hass):
    device_registry = dr.async_get(hass)
    device = device_registry.async_get(device_id)
    identifiers = {identifier[1] for identifier in device.identifiers
                   if len(identifier) >= 2 and identifier[0] == DOMAIN} if device else set()
    if len(identifiers) != 1:
        raise HomeAssistantError("Unknown or ambiguous hOn target")
    return identifiers.pop()
