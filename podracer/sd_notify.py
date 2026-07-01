"""Minimal sd_notify(3) client — stdlib only, no python-systemd dependency.

systemd hands a service its notify socket path in NOTIFY_SOCKET. We send the
worker's readiness ("READY=1") and periodic liveness pings ("WATCHDOG=1") to it
so a hung loop (no pings) gets killed and restarted once the unit sets
WatchdogSec. Everything here is best-effort: a notify failure must never take
down the worker, and outside systemd (dev, tests, the web process) NOTIFY_SOCKET
is unset so notify() is a silent no-op.
"""
import os
import socket


def notify(state: str) -> None:
    """Send a service-state line to systemd's notify socket (no-op if unset)."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    # Abstract-namespace sockets are reported with a leading '@'; the kernel
    # address starts with a NUL byte instead.
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(state.encode())
    except OSError:
        # Best-effort: never crash the worker over a failed notify.
        pass
