"""Runtime/fixture contract shared by production-compatible and modern lanes."""
from importlib.metadata import version
from pathlib import Path


async def test_hass_uses_official_fixture_in_modern_lane(hass, request):
    if version("homeassistant") == "2026.9.4":
        assert request._fixture_defs["hass"].func.__module__ == "conftest"
    else:
        assert request._fixture_defs["hass"].func.__module__ == "pytest_homeassistant.plugins"
    assert Path(hass.config.config_dir).is_dir()
