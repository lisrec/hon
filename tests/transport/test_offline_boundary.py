"""Characterize the loopback boundary even when HA enables IP socket creation."""
import socket

import pytest


@pytest.mark.parametrize("family,host", [(socket.AF_INET, "192.0.2.1"),
                                        (socket.AF_INET6, "2001:db8::1")])
def test_socket_guard_denies_non_loopback(family, host):
    with (
        socket.socket(family, socket.SOCK_STREAM) as client,
        pytest.raises(AssertionError, match="Non-loopback denied"),
    ):
        client.connect((host, 443))
