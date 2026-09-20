"""Authenticated, session-scoped local control; bounded reads, no fixed global port."""
from __future__ import annotations

import ctypes
import hashlib
import hmac
import json
import os
import secrets
import socket
import threading
import time
from pathlib import Path


def session_scope() -> str:
    session = ctypes.c_ulong()
    if os.name == "nt":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.ProcessIdToSessionId.argtypes = [ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
        kernel.ProcessIdToSessionId.restype = ctypes.c_int
        if not kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
            raise ctypes.WinError(ctypes.get_last_error())
    identity = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "user")
    return f"{hashlib.sha256(identity.encode()).hexdigest()[:16]}-{session.value}"


def send(directory: Path, command: str, unprotect) -> bool:
    if command not in {"SHOW_SETTINGS", "PING", "SHUTDOWN"}:
        return False
    try:
        metadata = directory / f"ipc-{session_scope()}.json"
        if metadata.stat().st_size > 4096:
            return False
        info = json.loads(metadata.read_text(encoding="utf-8"))
        token = unprotect(info["token"])
        if not token:
            return False
        data = json.dumps({"token": token, "command": command}).encode()
        with socket.create_connection(("127.0.0.1", int(info["port"])), timeout=0.5) as client:
            client.sendall(data + b"\n")
            return client.recv(16) == b"OK\n"
    except (OSError, ValueError, KeyError, TypeError):
        return False


def serve(directory: Path, stop: threading.Event, callback, protect) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    metadata = directory / f"ipc-{session_scope()}.json"
    token = secrets.token_hex(32)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(5)
        server.settimeout(0.5)
        metadata.write_text(json.dumps({"port": server.getsockname()[1], "token": protect(token)}), encoding="utf-8")
        last_action = 0.0
        try:
            while not stop.is_set():
                try:
                    client, _ = server.accept()
                except socket.timeout:
                    continue
                with client:
                    client.settimeout(0.3)
                    try:
                        data = b""
                        deadline = time.monotonic() + 0.5
                        while b"\n" not in data and len(data) < 512:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise TimeoutError("Local command deadline exceeded")
                            client.settimeout(min(0.3, remaining))
                            part = client.recv(512 - len(data))
                            if not part:
                                break
                            data += part
                        request = json.loads(data)
                        if not isinstance(request, dict) or not isinstance(request.get("token"), str):
                            continue
                        if not hmac.compare_digest(request["token"], token):
                            continue
                        command = request.get("command")
                        if command not in {"PING", "SHOW_SETTINGS", "SHUTDOWN"}:
                            continue
                        now = time.monotonic()
                        if command != "PING":
                            if now - last_action < 0.5:
                                client.sendall(b"BUSY\n")
                                continue
                            callback(command)
                            last_action = now
                        client.sendall(b"OK\n")
                    except (OSError, ValueError, TypeError):
                        continue
        finally:
            try:
                metadata.unlink(missing_ok=True)
            except OSError:
                pass
