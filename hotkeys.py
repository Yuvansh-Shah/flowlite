"""Global hotkey state machine.

States: IDLE, RECORDING (push-to-talk), LOCKED (hands-free).

Transitions:
  IDLE  + record-held edge        -> RECORDING (start mic)
  IDLE  + lock-toggle tap         -> LOCKED    (start mic)
  REC   + record released         -> IDLE      (stop -> transcribe -> paste)
  REC   + lock-toggle tap         -> LOCKED    (promote, keep recording)
  LOCK  + lock-toggle tap         -> IDLE      (stop -> transcribe -> paste)
  LOCK  + record tap              -> IDLE      (stop -> transcribe -> paste)
  REC/LOCK + cancel tap           -> IDLE      (discard)

Detection layers:
  1. 50Hz polling loop covers record hold/release, cancel, retrieve.
  2. A `keyboard.on_press_key("space")` hook covers lock-toggle reliably
     even when Windows intercepts Ctrl+Win+Space for input-language
     switching before our polling can see it.
  3. `sync_external_state()` lets the daemon mirror its own recording
     state (started via pill click) into this controller so the space
     hook works regardless of how recording was initiated.
"""
from __future__ import annotations

import enum
import sys
import threading
import time
from typing import Callable

import keyboard

from config import HotkeyConfig


class State(enum.Enum):
    IDLE = "idle"
    RECORDING = "recording"
    LOCKED = "locked"


# Some key aliases differ between the `keyboard` library's naming and casual config.
_KEY_ALIASES = {
    "win": ("left windows", "right windows"),
    "windows": ("left windows", "right windows"),
    "cmd": ("left windows", "right windows"),
    "ctrl": ("left ctrl", "right ctrl"),
    "control": ("left ctrl", "right ctrl"),
    "alt": ("left alt", "right alt"),
    "shift": ("left shift", "right shift"),
}


def _parse_combo(combo: str) -> tuple[str, ...]:
    return tuple(k.strip().lower() for k in combo.split("+"))


def _key_pressed(key: str) -> bool:
    aliases = _KEY_ALIASES.get(key)
    if aliases:
        return any(_safe_is_pressed(a) for a in aliases) or _safe_is_pressed(key)
    return _safe_is_pressed(key)


def _safe_is_pressed(key: str) -> bool:
    try:
        return keyboard.is_pressed(key)
    except Exception:
        return False


def _all_pressed(keys: tuple[str, ...]) -> bool:
    return all(_key_pressed(k) for k in keys)


class HotkeyController:
    def __init__(
        self,
        cfg: HotkeyConfig,
        on_start: Callable[[State], None],
        on_stop: Callable[[], None],
        on_cancel: Callable[[], None],
        on_retrieve: Callable[[], None] | None = None,
        on_lock_promoted: Callable[[], None] | None = None,
    ):
        self.cfg = cfg
        self._on_start = on_start
        self._on_stop = on_stop
        self._on_cancel = on_cancel
        self._on_retrieve = on_retrieve or (lambda: None)
        # Called when state goes RECORDING -> LOCKED (so UI can show indicator).
        self._on_lock_promoted = on_lock_promoted or (lambda: None)

        self._record_combo = _parse_combo(cfg.record)
        self._lock_combo = _parse_combo(cfg.lock_toggle)
        self._cancel_combo = _parse_combo(cfg.cancel)
        self._retrieve_combo = _parse_combo(cfg.retrieve_paste)
        # The lock-combo modifiers other than "space" — used by the space hook.
        self._lock_modifiers = tuple(
            k for k in self._lock_combo if k.lower() != "space"
        )

        self._state = State.IDLE
        # RLock because the polling thread acquires this lock and then calls
        # callbacks (e.g. on_stop -> daemon.stop_recording -> sync_external_state)
        # that try to re-acquire it from the same thread. A regular Lock would
        # deadlock — this is exactly the bug that left the daemon silently
        # stuck after pill-click recordings.
        self._state_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._space_hook = None

        self._was_record = False
        self._was_lock = False
        self._was_cancel = False
        self._was_retrieve = False
        # True when the polling loop itself put us into RECORDING via a
        # ctrl+win hold. Pill-click recordings set this False — without
        # it the polling loop would see "state=RECORDING but ctrl+win not
        # held" on its very next tick and immediately stop the recording.
        self._started_by_hotkey = False

    @property
    def state(self) -> State:
        return self._state

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="hotkey-watcher", daemon=True
        )
        self._thread.start()
        # Register a low-level key-down hook for space so we can detect
        # the lock toggle even when Windows eats the simultaneous combo.
        try:
            self._space_hook = keyboard.on_press_key(
                "space", self._on_space_down, suppress=False
            )
        except Exception as e:
            print(f"[hotkeys] couldn't register space hook: {e}", file=sys.stderr)
        print(
            f"[hotkeys] watching: record={self.cfg.record!r} "
            f"lock={self.cfg.lock_toggle!r} cancel={self.cfg.cancel!r} "
            f"retrieve={self.cfg.retrieve_paste!r}",
            file=sys.stderr,
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._space_hook is not None:
            try:
                keyboard.unhook(self._space_hook)
            except Exception:
                pass
            self._space_hook = None
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None

    def sync_external_state(self, recording: bool) -> None:
        """Called by the daemon when recording starts/stops outside the
        hotkey path (i.e. pill button click). Lets the space hook still
        recognise lock-toggle when recording was started by clicking."""
        with self._state_lock:
            if recording and self._state == State.IDLE:
                self._state = State.RECORDING
                # Marker: this RECORDING wasn't started by holding ctrl+win,
                # so the polling loop must NOT stop it just because ctrl+win
                # isn't currently held.
                self._started_by_hotkey = False
                print("[hotkeys] external sync -> RECORDING (pill-initiated)", file=sys.stderr)
            elif not recording and self._state != State.IDLE:
                self._state = State.IDLE
                self._started_by_hotkey = False
                print("[hotkeys] external sync -> IDLE", file=sys.stderr)

    # ------------------------- space hook --------------------------------

    def _on_space_down(self, _event) -> None:
        """Fires immediately when space is pressed, on the keyboard library's
        own hook thread. Cheaper to react here than wait for the next poll,
        and survives Windows pre-empting Ctrl+Win+Space."""
        with self._state_lock:
            state = self._state
            if state not in (State.RECORDING, State.LOCKED):
                return
            # Require at least one non-space lock-combo key to be held —
            # otherwise a normal space keypress in another app would trigger.
            if not any(_key_pressed(k) for k in self._lock_modifiers):
                return
            if state == State.RECORDING:
                self._state = State.LOCKED
                print(
                    "[hotkeys] space-hook: RECORDING -> LOCKED (hands-free)",
                    file=sys.stderr,
                )
                try:
                    self._on_lock_promoted()
                except Exception as e:
                    print(f"[hotkeys] on_lock_promoted error: {e}", file=sys.stderr)
            elif state == State.LOCKED:
                self._state = State.IDLE
                print("[hotkeys] space-hook: LOCKED -> IDLE (stop)", file=sys.stderr)
                try:
                    self._on_stop()
                except Exception as e:
                    print(f"[hotkeys] on_stop error: {e}", file=sys.stderr)

    # ------------------------- polling loop ------------------------------

    def _loop(self) -> None:
        # 50 Hz poll is responsive enough for hold detection and tiny CPU impact.
        interval = 0.02
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                print(f"[hotkeys] tick error: {e}", file=sys.stderr)
            time.sleep(interval)

    def _tick(self) -> None:
        lock_held = _all_pressed(self._lock_combo)
        record_held = _all_pressed(self._record_combo) and not lock_held
        cancel_held = _all_pressed(self._cancel_combo)
        retrieve_held = _all_pressed(self._retrieve_combo)

        lock_edge = lock_held and not self._was_lock
        record_edge = record_held and not self._was_record
        cancel_edge = cancel_held and not self._was_cancel
        retrieve_edge = retrieve_held and not self._was_retrieve

        self._was_lock = lock_held
        self._was_record = record_held
        self._was_cancel = cancel_held
        self._was_retrieve = retrieve_held

        # Retrieve is stateless — fires regardless of recording state.
        if retrieve_edge:
            print(
                f"[hotkeys] retrieve edge ({self.cfg.retrieve_paste})",
                file=sys.stderr,
            )
            self._on_retrieve()
            return

        with self._state_lock:
            # Cancel always wins.
            if cancel_edge and self._state != State.IDLE:
                self._state = State.IDLE
                self._on_cancel()
                return

            if lock_edge:
                if self._state == State.IDLE:
                    self._state = State.LOCKED
                    self._on_start(State.LOCKED)
                elif self._state == State.LOCKED:
                    self._state = State.IDLE
                    self._on_stop()
                elif self._state == State.RECORDING:
                    self._state = State.LOCKED
                    print(
                        "[hotkeys] poll: RECORDING -> LOCKED via full combo",
                        file=sys.stderr,
                    )
                    try:
                        self._on_lock_promoted()
                    except Exception as e:
                        print(
                            f"[hotkeys] on_lock_promoted error: {e}",
                            file=sys.stderr,
                        )
                return

            if self._state == State.IDLE and record_held:
                self._state = State.RECORDING
                self._started_by_hotkey = True
                self._on_start(State.RECORDING)
                return

            # Only auto-stop on release when WE started the recording via
            # the hotkey hold. Pill-initiated recordings stay alive until
            # the user clicks stop.
            if (
                self._state == State.RECORDING
                and not record_held
                and self._started_by_hotkey
            ):
                self._state = State.IDLE
                self._started_by_hotkey = False
                self._on_stop()
                return

            if self._state == State.LOCKED and record_edge:
                self._state = State.IDLE
                self._started_by_hotkey = False
                self._on_stop()
                return
