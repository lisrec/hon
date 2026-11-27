# Offline test lanes

Use Python 3.14 (modern Core requires >=3.14.2), with a separate virtualenv per
requirements file. Install using ordinary `python -m pip install -r FILE`, then
`python -m pip check` and `python -m pytest -q`. Do not combine both requirements
files or override a Core-owned dependency with `--no-deps`.

| Lane | Requirements | Core | aiohttp | Fixtures |
| --- | --- | --- | --- | --- |
| Production compatibility | `requirements-test.txt` | 2026.9.4 | 3.14.3 | Existing minimal real-HA fixture |
| Modern development | `requirements-dev.txt` | 2026.11.3 | 3.14.5 | Official matching `pytest-homeassistant==2026.11.3` |

The modern file uses the conventional root `requirements-dev.txt` name so the
nightly dependency inventory and bounded PyPI fallback discover it alongside
`requirements-test.txt`. Do not combine these two environments. Calendar-daily
Dependabot configuration alone does not prove the fork updater has executed;
the pip fallback does not cover GitHub Actions version checks.

The legacy pytest 9.2.0/pytest-asyncio 1.5.0 pins remain unchanged. The official
modern fixture requires pytest 9.0.3/pytest-asyncio 1.4.0 exactly; this deliberate
lane-local pair is not a resolver downgrade of the production lane.

Modern tests use the official `hass` fixture, its cleanup checks and temporary
configuration directory. Registry setup is performed only when not already
provided by the fixture. respx's standalone plugin is disabled as required by
the official fixtures, without disabling their HTTP mocks. aioresponses still
rejects unregistered requests; transport tests selectively allow socket creation
but keep the existing loopback-only connect guard. TLS verification remains
mandatory. The intentional untrusted-certificate test consumes only its expected
server-side TLS handshake reset; unrelated loop exceptions still fail the test.

## Security scope

This is test infrastructure, **not a production upgrade or runtime remediation**.
Core 2026.9.4 pins aiohttp 3.14.3; its known advisories remain in the retained
compatibility lane. Follow-up is owned by
[issue 3](https://github.com/lisrec/hon/issues/3), which must stay open even if
the modern test environment has no known audit findings. Other Core-owned
legacy transitives can have findings too; do not override them independently
or describe the modern audit as a clearance of the running home.

The integration source, appliance protocols, quiet/fences and release contents
are unchanged. Offline tests do not establish physical appliance behavior.
No cloud login, device actuation, HA restart, release or deployment is implied.
