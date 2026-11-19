"""Offline manifest/CI contract; no package installation or GitHub access."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_every_ci_lane_is_visible_to_daily_dependency_inventory():
    """Use conventional root test/dev names supported by the nightly fallback."""
    workflow = (ROOT / ".github/workflows/tests.yml").read_text()
    lanes = re.findall(r"^\s+requirements: (\S+)$", workflow, re.MULTILINE)
    assert len(lanes) == 2
    supported = {"requirements-test.txt", "requirements_test.txt",
                 "requirements-dev.txt", "requirements_dev.txt"}
    for lane in lanes:
        assert lane in supported, f"CI lane omitted from daily dependency inventory: {lane}"
        assert (ROOT / lane).is_file()


def test_modern_lane_keeps_core_owned_patched_aiohttp_and_official_fixture():
    requirements = (ROOT / "requirements-dev.txt").read_text().splitlines()
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
    assert "requirements-dev.txt" in workflow
    assert "fail-fast: false" in workflow
    assert "pip check" in workflow
    assert "python -m pytest -q" in workflow
    assert "-p no:respx" in (ROOT / "pytest.ini").read_text()
