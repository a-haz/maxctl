from __future__ import annotations

import json
import os
import socket
from pathlib import Path

from maxctl.const import SOCKET_PATH


class HelperError(RuntimeError):
    pass


def socket_path() -> str:
    return os.environ.get("MAXCTL_SOCKET", SOCKET_PATH)


def connected() -> bool:
    return Path(socket_path()).exists()


def request(cmd: str, **fields) -> dict:
    path = socket_path()
    if not Path(path).exists():
        raise HelperError(
            "maxctld is not running. Install the helper with: sudo ./scripts/install.sh"
        )
    payload = {"cmd": cmd, **fields}
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(5)
        sock.connect(path)
        sock.sendall((json.dumps(payload) + "\n").encode())
        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
            if b"\n" in data:
                break
        raw = b"".join(chunks).decode().strip()
        if not raw:
            raise HelperError("empty reply from maxctld")
        reply = json.loads(raw)
    except OSError as exc:
        raise HelperError(f"cannot talk to maxctld: {exc}") from exc
    finally:
        sock.close()
    if not reply.get("ok", False) and cmd != "status":
        raise HelperError(reply.get("error") or "helper rejected the request")
    return reply
