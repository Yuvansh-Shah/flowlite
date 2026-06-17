"""Thread-safe ring of recent transcripts.

Every successful transcription is added here. The retrieve hotkey
(Ctrl+Alt+V by default) pastes whatever was added most recently — useful
when the original paste landed in a window that ignored it (desktop,
taskbar, an app with no text field focused, etc.).

Peek-style API: `latest()` returns the last entry without removing it,
so you can paste it multiple times.
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Optional


class PasteQueue:
    def __init__(self, max_items: int = 10):
        self._items: deque[str] = deque(maxlen=max_items)
        self._lock = threading.Lock()

    def add(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._items.append(text)

    def latest(self) -> Optional[str]:
        with self._lock:
            return self._items[-1] if self._items else None

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
