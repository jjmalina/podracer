"""sd_notify helper: no-op without NOTIFY_SOCKET, real datagram with one set.

Covers the three behaviors the worker relies on: silent no-op in dev/tests,
sending the exact bytes to a path socket, and the abstract-namespace ('@' ->
leading NUL) address rewrite.
"""
import socket

from podracer.sd_notify import notify


def test_notify_noop_without_socket(monkeypatch):
    """No NOTIFY_SOCKET set -> silent no-op, never raises."""
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    notify("READY=1")  # must not raise


def test_notify_sends_datagram_to_path_socket(tmp_path, monkeypatch):
    sock_path = str(tmp_path / "notify.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(sock_path)
    server.settimeout(1.0)
    try:
        monkeypatch.setenv("NOTIFY_SOCKET", sock_path)
        notify("WATCHDOG=1")
        assert server.recv(64) == b"WATCHDOG=1"
    finally:
        server.close()


def test_notify_handles_abstract_namespace(monkeypatch):
    """A '@'-prefixed address is an abstract socket: the kernel name is the
    leading NUL plus the rest."""
    name = "podracer-test-notify"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind("\0" + name)  # abstract namespace
    server.settimeout(1.0)
    try:
        monkeypatch.setenv("NOTIFY_SOCKET", "@" + name)
        notify("READY=1")
        assert server.recv(64) == b"READY=1"
    finally:
        server.close()


def test_notify_swallows_send_errors(monkeypatch):
    """A dead/missing socket path must not crash the caller."""
    monkeypatch.setenv("NOTIFY_SOCKET", "/nonexistent/podracer/notify.sock")
    notify("WATCHDOG=1")  # connect() fails -> swallowed
