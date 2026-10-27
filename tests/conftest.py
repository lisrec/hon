"""Real HA runtime, isolated storage and fail-closed HTTP for offline tests."""

import re
from pathlib import Path
from types import MappingProxyType
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiohttp import ClientResponse
from aiohttp.abc import AbstractStreamWriter
from aioresponses import aioresponses
from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import frame
from homeassistant.loader import async_setup

from custom_components.hon import async_setup_entry
from custom_components.hon.base import HonBaseCoordinator
from custom_components.hon.const import API_URL
from custom_components.hon.device import HonDevice
from custom_components.hon.hon import HonConnection

APPLIANCE = {
    "macAddress": "00:00:00:00:00:01",
    "applianceTypeName": "AC",
    "applianceTypeId": 11,
    "brand": "Offline",
    "modelName": "Test AC",
    "fwVersion": "test",
    "applianceModelId": "test",
    "serialNumber": "test",
    "connectivity": "wifi",
}


@pytest.fixture
def device(hass, connection):
    coordinator = HonBaseCoordinator(hass, connection, APPLIANCE.copy())
    appliance = HonDevice(connection, coordinator, APPLIANCE.copy())
    coordinator.device = appliance
    appliance._attributes = {
        "parameters": {
            "echoStatus": "0",
            "tempSel": "21",
            "tempIndoor": "20",
            "onOffStatus": "1",
            "machMode": "4",
            "windSpeed": "2",
            "windDirectionHorizontal": "0",
            "windDirectionVertical": "5",
        }
    }
    return appliance


class OfflineClientResponse(ClientResponse):
    """Bridge aioresponses 0.7.9 to HA's pinned aiohttp 3.14 signature."""

    def __init__(self, *args, **kwargs):
        # aiohttp now records the size of an already-sent request. No socket is used.
        super().__init__(
            *args,
            stream_writer=Mock(spec=AbstractStreamWriter, output_size=0),
            **kwargs,
        )


@pytest.fixture(autouse=True)
def http(monkeypatch):
    # Unregistered requests fail: never contact hOn or an appliance.
    monkeypatch.setattr("aioresponses.core.ClientResponse", OfflineClientResponse)
    with aioresponses() as mocked:
        yield mocked


@pytest.fixture
async def hass(tmp_path):
    (tmp_path / "custom_components").symlink_to(
        Path(__file__).resolve().parents[1] / "custom_components",
        target_is_directory=True,
    )
    instance = HomeAssistant(str(tmp_path))
    async_setup(instance)
    frame.async_setup(instance)
    instance.config_entries = ConfigEntries(instance, {})
    await instance.config_entries.async_initialize()
    yield instance
    await instance.async_stop(force=True)


@pytest.fixture
def entry(hass):
    config_entry = ConfigEntry(
        domain="hon",
        title="Offline account",
        data={"email": "offline@example.test", "password": "offline-only"},
        options={},
        source="user",
        unique_id="offline@example.test",
        version=1,
        minor_version=1,
        discovery_keys=MappingProxyType({}),
        subentries_data=[],
    )
    # Register without setting up the integration (which would log in).
    hass.config_entries._entries[config_entry.entry_id] = config_entry
    return config_entry


@pytest.fixture
async def connection(hass, entry):
    hon = HonConnection(hass, entry)
    yield hon
    await hon.async_close()


@pytest.fixture
def auth_http(http):
    """Simulate auth responses offline; no real account or token is used."""
    http.get(
        re.compile(re.escape(f"{API_URL}/ciam/authorize") + r"\?.*"),
        payload={"session_id": "offline"},
    )
    http.post(
        f"{API_URL}/ciam/token",
        payload={
            "tokens": {
                "cognito_token": "offline",
                "id_token": "offline",
                "refresh_token": "offline",
            }
        },
    )
    http.post(
        f"{API_URL}/unified-api/v1/view/appliance-list",
        payload={
            "modules": {"applianceList": {"payload": {"appliances": []}}},
        },
    )


@pytest.fixture
async def loaded_entry(hass, entry, auth_http):
    # Real integration setup and services; skip platforms with no appliances.
    with patch.object(
        hass.config_entries, "async_forward_entry_setups", new_callable=AsyncMock
    ):
        assert await async_setup_entry(hass, entry)
    entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    yield entry
    await hass.data["hon"][entry.unique_id].async_close()
