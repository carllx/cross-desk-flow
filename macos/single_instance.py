"""Single-instance locking mechanism for macOS controller.

Guarantees controller singleton execution per machine via local UDP socket bind.
"""

from __future__ import annotations

import socket
from typing import Optional

from bridge_core.contract import DEFAULT_SINGLETON_PORT


class SingleInstanceLock:
    """Guarantees controller singleton execution per machine via local UDP socket bind."""

    def __init__(self, port: int = DEFAULT_SINGLETON_PORT):
        self.port = port
        self._sock: Optional[socket.socket] = None
        self._held = False

    def acquire(self) -> bool:
        if self._held and self._sock:
            return True
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("127.0.0.1", self.port))
            self._sock = s
            self._held = True
            return True
        except (OSError, socket.error):
            self._sock = None
            self._held = False
            return False

    def release(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._held = False

    @property
    def is_held(self) -> bool:
        return self._held
