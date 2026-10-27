"""Pure policy invariants; transport tests exercise the same helper end to end."""

from copy import deepcopy

import pytest

from custom_components.hon.hon import _prepare_command_parameters


@pytest.mark.parametrize(
    "appliance_type,enabled,parameters,expected",
    [
        ("AC", True, {"tempSel": "21"}, {"tempSel": "21", "echoStatus": "1"}),
        ("AC", True, {"echoStatus": "0"}, {"echoStatus": "1"}),
        ("AC", True, {"echoStatus": "1"}, {"echoStatus": "1"}),
        ("AC", False, {"echoStatus": "0"}, {"echoStatus": "0"}),
        ("AC", False, {}, {}),
        ("WM", True, {"echoStatus": "0"}, {"echoStatus": "0"}),
        ("OV", True, {}, {}),
    ],
)
def test_policy_only_changes_ac_echo_on_a_copy(
    appliance_type, enabled, parameters, expected
):
    original = deepcopy(parameters)
    result = _prepare_command_parameters(
        appliance_type, parameters, quiet_ac_commands=enabled
    )
    assert result == expected
    assert result is not parameters
    assert parameters == original
