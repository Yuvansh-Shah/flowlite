"""Single-instance guard + tiny IPC over a localhost socket.

First launch  -> binds the port, becomes PRIMARY, listens for "show" pings.
Later launches -> fail to bind, send "show" to the primary so its window
                  pops to the front, then exit.

This replaces the old named-mutex approach so that clicking the app icon a
second time actually OPENS the already-running window instead of silently
doing nothing.
"""
from __future__ import annotations

import socket
import threading
from typing import Callable, Optional

_HOST = "127.0.0.1"
_PORT = 50573  # arbitrary high port, stable across launches
_MSG = b"show\n"


class SingleInstance:
    def __init__(self):
        self._srv: Optional[socket.socket] = None
        self.is_primary = False

    def acquire(self) -> bool:
        """Try to become the primary instance. Returns True if we are."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            s.bind((_HOST, _PORT))
            s.listen(5)
        except OSError:
            s.close()
            self.is_primary = False
            return False
        self._srv = s
        self.is_primary = True
        return True

    def start_listener(self, on_show: Callable[[], None]) -> None:
        """Primary only: run a thread that calls on_show() for each ping."""
        if not self.is_primary or self._srv is None:
            return

        def loop():
            while True:
                try:
                    conn, _ = self._srv.accept()
                except OSError:
                    break
                try:
                    conn.recv(64)
                    on_show()
                except Exception:
                    pass
                finally:
                    conn.close()

        threading.Thread(target=loop, name="singleton-listener", daemon=True).start()

    @staticmethod
    def signal_show() -> bool:
        """Secondary: tell the running primary to show its window."""
        try:
            with socket.create_connection((_HOST, _PORT), timeout=2) as c:
                c.sendall(_MSG)
            return True
        except OSError:
            return False

    def release(self) -> None:
        if self._srv is not None:
            try:
                self._srv.close()
            except Exception:
                pass
            self._srv = None
