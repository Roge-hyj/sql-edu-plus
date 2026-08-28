"""Pytest guardrails used only by the Phase 2 acceptance runner.

The acceptance suite is intentionally local and deterministic.  Blocking
Internet sockets here makes an accidental network dependency fail closed
instead of silently turning a freeze run into an online integration test.
Unix-domain sockets remain available because they are local IPC, although the
declared Phase 2 acceptance groups do not require them.
"""

from __future__ import annotations

import os
import socket
from typing import Any


_ORIGINAL_SOCKET_CONNECT = socket.socket.connect
_ORIGINAL_SOCKET_CONNECT_EX = socket.socket.connect_ex
_ORIGINAL_SOCKET_SENDTO = socket.socket.sendto
_ORIGINAL_CREATE_CONNECTION = socket.create_connection


def _offline_error(address: Any) -> OSError:
    return OSError(
        "Phase 2 acceptance is offline; outbound network access was blocked "
        f"for {type(address).__name__}"
    )


def _guarded_socket_connect(sock: socket.socket, address: Any) -> Any:
    if sock.family in {socket.AF_INET, socket.AF_INET6}:
        raise _offline_error(address)
    return _ORIGINAL_SOCKET_CONNECT(sock, address)


def _guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
    raise _offline_error(address)


def _guarded_socket_connect_ex(sock: socket.socket, address: Any) -> int:
    if sock.family in {socket.AF_INET, socket.AF_INET6}:
        raise _offline_error(address)
    return _ORIGINAL_SOCKET_CONNECT_EX(sock, address)


def _guarded_socket_sendto(sock: socket.socket, data: Any, *args: Any) -> int:
    address = args[-1] if args else None
    if sock.family in {socket.AF_INET, socket.AF_INET6} and address is not None:
        raise _offline_error(address)
    return _ORIGINAL_SOCKET_SENDTO(sock, data, *args)


def pytest_configure() -> None:
    """Install the guard only for an explicit acceptance subprocess."""
    if os.environ.get("PHASE2_ACCEPTANCE_OFFLINE") != "1":
        raise RuntimeError(
            "phase2_acceptance_guard may only run under the acceptance runner"
        )
    socket.socket.connect = _guarded_socket_connect
    socket.socket.connect_ex = _guarded_socket_connect_ex
    socket.socket.sendto = _guarded_socket_sendto
    socket.create_connection = _guarded_create_connection


def pytest_unconfigure() -> None:
    socket.socket.connect = _ORIGINAL_SOCKET_CONNECT
    socket.socket.connect_ex = _ORIGINAL_SOCKET_CONNECT_EX
    socket.socket.sendto = _ORIGINAL_SOCKET_SENDTO
    socket.create_connection = _ORIGINAL_CREATE_CONNECTION
