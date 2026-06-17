"""flowlite daemon — entry point.

Wires UI buttons + global hotkeys to the audio/STT/format/paste pipeline.
The Qt event loop runs on the main thread; transcription runs on a worker
thread so the UI stays responsive.

Recording control surface (both hotkey and pill-button paths converge here):
    start_recording()   begin fresh session
    pause_recording()   suspend mic capture, keep buffer
    resume_recording()  resume capture into the same buffer
    stop_recording()    end session, transcribe, format, paste
    cancel_recording()  discard without pasting
"""
from __future__ import annotations

import sys
import threading
import time
import traceback

# ── CRITICAL IMPORT ORDER ──────────────────────────────────────────────
# faster-whisper / ctranslate2 MUST be imported before PyQt6, otherwise the
# CUDA model load crashes with a Windows access violation (the two ship
# conflicting native DLLs; whichever loads first wins). `stt` pulls in
# faster_whisper, so it must come before `ui` / `app_window` (PyQt6).
from stt import Transcriber  # noqa: E402  (keep first — see note above)
# ───────────────────────────────────────────────────────────────────────

import config as config_mod
import singleton
from audio import Recorder
from formatter import Formatter
from hotkeys import HotkeyController, State as HotkeyState
from app_window import MainWindow
from dictionary import Dictionary
from output import PasteResult, paste
from paste_queue import PasteQueue
from ui import TrayApp


class Daemon:
    def __init__(self):
        self.cfg = config_mod.load()
        self.dictionary = Dictionary()
        self.recorder = Recorder(self.cfg.audio)
        self.transcriber = Transcriber(self.cfg.stt)
        self.formatter = Formatter(self.cfg.formatter, dictionary=self.dictionary)

        # CRITICAL ORDER: load the Whisper / CTranslate2 CUDA model BEFORE any
        # Qt object (QApplication) exists. Creating the CUDA context after Qt
        # has initialised reliably triggers a Windows access-violation crash
        # (Qt and CTranslate2 fight over some shared DLL / GPU state). Loading
        # first — with no Qt in the process yet — is rock solid.
        try:
            self.transcriber.load()
        except Exception as e:
            print(f"[daemon] whisper preload failed: {e}", file=sys.stderr)

        self.tray = TrayApp(
            self.cfg.ui,
            callbacks={
                "start": self.start_recording,
                "stop": self.stop_recording,
                "cancel": self.cancel_recording,
                "quit": self.shutdown,
            },
            get_audio_level=self._current_audio_level,
            hotkey_label=self.cfg.hotkeys.record,
        )
        self.hotkeys = HotkeyController(
            self.cfg.hotkeys,
            on_start=self._hotkey_start,
            on_stop=self.stop_recording,
            on_cancel=self.cancel_recording,
            on_retrieve=self.retrieve_paste,
            on_lock_promoted=self._lock_promoted,
        )
        self.queue = PasteQueue(max_items=10)
        self._worker_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._total_words = 0

        # Main app window (Flow-style). Hidden until the user opens it from
        # the tray or the desktop shortcut.
        self.window = MainWindow(
            dictionary=self.dictionary, cfg=self.cfg, on_quit=self._quit_from_window
        )
        self.tray.attach_window(self.window)
        self._singleton = None

    def attach_singleton(self, guard) -> None:
        self._singleton = guard
        # When a second launch pings us, pop the window to the front.
        # The ping arrives on a socket thread, so route through the Qt
        # signal so the show happens on the GUI thread.
        guard.start_listener(self.tray.bridge.sig_show_window.emit)

    def start(self) -> int:
        # Whisper is already loaded (in __init__, before Qt existed). Just warm
        # Ollama on a thread so it doesn't delay the UI.
        threading.Thread(
            target=self.formatter.warmup, name="ollama-warmup", daemon=True
        ).start()
        self.hotkeys.start()
        # Open the main window on launch so the user sees the app, not just a
        # tray icon they have to hunt for.
        self.window.show_front()
        return self.tray.run()

    def shutdown(self) -> None:
        self.hotkeys.stop()
        if self.recorder.is_recording:
            self.recorder.discard()

    # ----------------------- recording control --------------------------

    def _hotkey_start(self, _mode: HotkeyState) -> None:
        self.start_recording()

    def start_recording(self) -> None:
        with self._state_lock:
            if self.recorder.is_recording:
                return
            try:
                self.recorder.start()
            except Exception as e:
                print(f"[daemon] mic open failed: {e}", file=sys.stderr)
                self.tray.bridge.sig_idle.emit()
                return
        self.tray.bridge.sig_recording.emit(False)
        self.window.bridge.sig_status.emit("recording")
        # Mirror the recording state into the hotkey controller so
        # space-tap lock-toggle works even when started via pill click.
        self.hotkeys.sync_external_state(recording=True)
        print("[daemon] recording", file=sys.stderr)

    def _quit_from_window(self) -> None:
        self.tray.bridge.sig_quit.emit()

    def _lock_promoted(self) -> None:
        """Called by hotkey controller when state transitions
        RECORDING -> LOCKED (hands-free)."""
        self.tray.bridge.sig_locked.emit()
        print("[daemon] promoted to hands-free (locked)", file=sys.stderr)

    def pause_recording(self) -> None:
        with self._state_lock:
            if not self.recorder.is_recording or self.recorder.is_paused:
                return
            self.recorder.pause()
        self.tray.bridge.sig_paused.emit()
        print("[daemon] paused", file=sys.stderr)

    def resume_recording(self) -> None:
        with self._state_lock:
            if not self.recorder.is_recording or not self.recorder.is_paused:
                return
            self.recorder.resume()
        self.tray.bridge.sig_recording.emit(True)
        print("[daemon] resumed", file=sys.stderr)

    def stop_recording(self) -> None:
        with self._state_lock:
            if not self.recorder.is_recording:
                return
            audio = self.recorder.stop()
        self.hotkeys.sync_external_state(recording=False)

        # Skip transcription entirely for "tap record / tap stop" misfires —
        # otherwise the UI spins on the transcribing animation for several
        # seconds while Whisper warms up to process a few empty samples.
        min_samples = int(self.cfg.audio.min_seconds * self.cfg.audio.sample_rate)
        if audio.size < min_samples:
            duration_s = audio.size / max(1, self.cfg.audio.sample_rate)
            print(
                f"[daemon] recording too short ({duration_s:.2f}s < "
                f"{self.cfg.audio.min_seconds:.2f}s) — skipping transcription",
                file=sys.stderr,
            )
            self.tray.bridge.sig_idle.emit()
            self.window.bridge.sig_status.emit("idle")
            return

        self.tray.bridge.sig_processing.emit()
        self.window.bridge.sig_status.emit("transcribing")
        threading.Thread(
            target=self._transcribe_and_paste,
            args=(audio,),
            name="transcribe",
            daemon=True,
        ).start()

    def cancel_recording(self) -> None:
        with self._state_lock:
            if not self.recorder.is_recording:
                return
            self.recorder.discard()
        self.tray.bridge.sig_idle.emit()
        self.window.bridge.sig_status.emit("idle")
        self.hotkeys.sync_external_state(recording=False)
        print("[daemon] cancelled", file=sys.stderr)

    def _current_audio_level(self) -> float:
        if self.recorder.is_recording and not self.recorder.is_paused:
            return self.recorder.current_level()
        return 0.0

    def retrieve_paste(self) -> None:
        """Hotkey-triggered paste of the most recent transcript.
        Bypasses the focus check — the user explicitly invoked this from
        a real text field, so we trust them."""
        text = self.queue.latest()
        print(
            f"[daemon] retrieve_paste invoked: queue_size={len(self.queue)} "
            f"latest_chars={len(text) if text else 0}",
            file=sys.stderr,
        )
        if not text:
            self.tray.bridge.sig_notify.emit(
                "flowlite",
                "Nothing to paste yet — record something first.",
                "info",
            )
            return
        # Give the user a moment to release Ctrl+Alt+V before we send Ctrl+V,
        # otherwise the OS sees the modifiers still held.
        threading.Thread(
            target=self._retrieve_paste_worker,
            args=(text,),
            name="retrieve-paste",
            daemon=True,
        ).start()

    def _retrieve_paste_worker(self, text: str) -> None:
        # Wait for the user to release Ctrl+Alt+V before we send Ctrl+V,
        # otherwise the modifiers leak into the synthesized paste.
        import keyboard as kb
        deadline = time.time() + 0.6
        while time.time() < deadline and (
            kb.is_pressed("ctrl") or kb.is_pressed("alt") or kb.is_pressed("v")
        ):
            time.sleep(0.02)
        time.sleep(0.05)
        result = paste(text, self.cfg.output, force=True)
        print(
            f"[daemon] retrieve-paste result={result.value} chars={len(text)}",
            file=sys.stderr,
        )
        if result != PasteResult.PASTED:
            self.tray.bridge.sig_notify.emit(
                "flowlite",
                "Retry paste failed — clipboard error.",
                "warning",
            )

    # ------------------------ worker pipeline ---------------------------

    def _transcribe_and_paste(self, audio) -> None:
        # Outer try/finally guarantees sig_idle fires even if the worker
        # lock blocks forever or anything below crashes — so the UI can
        # never end up permanently stuck on the transcribing animation.
        try:
            if not self._worker_lock.acquire(timeout=60):
                print("[daemon] worker lock timeout, abandoning", file=sys.stderr)
                return
            try:
                t0 = time.time()
                if audio.size == 0:
                    print("[daemon] no audio captured", file=sys.stderr)
                    return
                # Reload dictionary each run so edits take effect without
                # restarting, and bias Whisper toward the user's vocabulary.
                self.dictionary.reload()
                raw = self.transcriber.transcribe(
                    audio, initial_prompt=self.dictionary.initial_prompt()
                )
                t_stt = time.time() - t0
                if not raw:
                    print("[daemon] empty transcription", file=sys.stderr)
                    return
                cleaned = self.formatter.format(raw)
                t_total = time.time() - t0
                # Always queue first — paste might fail/skip, retrieve hotkey
                # is the fallback.
                self.queue.add(cleaned)
                # Update the app window: word count + last transcript.
                self._total_words += len(cleaned.split())
                self.window.bridge.sig_words.emit(self._total_words)
                self.window.bridge.sig_last.emit(cleaned)
                result = paste(cleaned, self.cfg.output)
                print(
                    f"[daemon] stt={t_stt:.2f}s total={t_total:.2f}s "
                    f"chars={len(cleaned)} paste={result.value}",
                    file=sys.stderr,
                )
                if result == PasteResult.SKIPPED and self.cfg.output.notify_on_skip:
                    self.tray.bridge.sig_notify.emit(
                        "flowlite — paste skipped",
                        f"No text field was focused. "
                        f"Click into one and press "
                        f"{_pretty_hotkey(self.cfg.hotkeys.retrieve_paste)} to paste.",
                        "warning",
                    )
                elif result == PasteResult.FAILED:
                    self.tray.bridge.sig_notify.emit(
                        "flowlite — paste error",
                        f"Clipboard or keyboard call failed. "
                        f"Press {_pretty_hotkey(self.cfg.hotkeys.retrieve_paste)} to try again.",
                        "warning",
                    )
            except Exception:
                traceback.print_exc()
            finally:
                self._worker_lock.release()
        finally:
            self.tray.bridge.sig_idle.emit()
            self.window.bridge.sig_status.emit("idle")


def _pretty_hotkey(combo: str) -> str:
    pretty_map = {
        "ctrl": "Ctrl", "control": "Ctrl",
        "windows": "Win", "win": "Win", "cmd": "Cmd",
        "alt": "Alt", "shift": "Shift", "space": "Space",
        "esc": "Esc", "tab": "Tab", "enter": "Enter",
    }
    parts = [pretty_map.get(p.strip().lower(), p.strip().title()) for p in combo.split("+")]
    return "+".join(parts)


def main() -> int:
    guard = singleton.SingleInstance()
    if not guard.acquire():
        # Already running: tell it to show its window, then exit.
        print("[flowlite] already running — opening its window.", file=sys.stderr)
        singleton.SingleInstance.signal_show()
        return 0
    daemon = Daemon()
    daemon.attach_singleton(guard)
    try:
        return daemon.start()
    except KeyboardInterrupt:
        daemon.shutdown()
        return 0


if __name__ == "__main__":
    sys.exit(main())
