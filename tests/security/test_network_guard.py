import socket

import pytest


def test_public_hosts_cannot_be_resolved_or_reached_from_tests():
    with pytest.raises(OSError, match="tests may not resolve public host"):
        socket.getaddrinfo("email.us-east-1.amazonaws.com", 443)
    with pytest.raises(OSError, match="tests may not connect"):
        socket.create_connection(("1.1.1.1", 443), timeout=1)
    with pytest.raises(OSError, match="tests may not connect"):
        socket.create_connection(("169.254.169.254", 80), timeout=1)
    assert socket.getaddrinfo("169.254.169.254", 80), "IP literals resolve locally so SSRF checks can run"
    assert socket.getaddrinfo("localhost", 80)
