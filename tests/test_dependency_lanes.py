"""Offline manifest/CI contract; no package installation or GitHub access."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_modern_lane_keeps_core_owned_patched_aiohttp_and_official_fixture():
    requirements = (ROOT / "requirements-test-modern.txt").read_text().splitlines()
    for pin in ("homeassistant==2026.11.2", "pytest-homeassistant==2026.11.2",
                "aiohttp==3.14.5", "pytest==9.0.3", "pytest-asyncio==1.4.0"):
        assert pin in requirements
    legacy = (ROOT / "requirements-test.txt").read_text().splitlines()
    for pin in ("homeassistant==2026.9.4", "aiohttp==3.14.3",
                "pytest==9.2.0", "pytest-asyncio==1.5.0"):
        assert pin in legacy
    assert not any(line.startswith("pytest-homeassistant") for line in legacy)


def test_ci_executes_both_resolved_lanes():
    workflow = (ROOT / ".github/workflows/tests.yml").read_text()
    assert "requirements-test.txt" in workflow
    assert "requirements-test-modern.txt" in workflow
    assert "fail-fast: false" in workflow
    assert "pip check" in workflow
    assert "python -m pytest -q" in workflow
    assert "-p no:respx" in (ROOT / "pytest.ini").read_text()
