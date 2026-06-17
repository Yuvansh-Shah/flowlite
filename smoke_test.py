"""Headless smoke test — verifies every component loads.
Does NOT exercise hotkeys or the Qt event loop (run daemon.py for that)."""
from __future__ import annotations

import sys
import time


def main() -> int:
    print("[smoke] importing modules...")
    import config as config_mod
    from audio import Recorder
    from formatter import Formatter
    from hotkeys import HotkeyController
    from output import paste  # noqa: F401
    from stt import Transcriber

    print("[smoke] loading config...")
    cfg = config_mod.load()
    print(f"        record hotkey:   {cfg.hotkeys.record!r}")
    print(f"        lock hotkey:     {cfg.hotkeys.lock_toggle!r}")
    print(f"        STT model:       {cfg.stt.model!r}")
    print(f"        STT device:      {cfg.stt.device!r} ({cfg.stt.compute_type})")
    print(f"        Formatter model: {cfg.formatter.model!r} (enabled={cfg.formatter.enabled})")

    print("[smoke] constructing Recorder...")
    Recorder(cfg.audio)
    print("[smoke] constructing Transcriber + loading model (this may take a moment)...")
    t0 = time.time()
    transcriber = Transcriber(cfg.stt)
    transcriber.load()
    print(f"        Whisper ready in {time.time()-t0:.1f}s")

    print("[smoke] constructing Formatter + warming Ollama...")
    t0 = time.time()
    formatter = Formatter(cfg.formatter)
    ok = formatter.warmup()
    print(f"        Ollama warmup {'OK' if ok else 'FAILED'} in {time.time()-t0:.1f}s")

    print("[smoke] constructing HotkeyController (not starting)...")
    HotkeyController(cfg.hotkeys, on_start=lambda *_: None, on_stop=lambda: None, on_cancel=lambda: None)

    print("[smoke] testing Whisper on synthetic silence (1s)...")
    import numpy as np
    silence = np.zeros(16000, dtype=np.float32)
    t0 = time.time()
    text = transcriber.transcribe(silence)
    print(f"        result={text!r} in {time.time()-t0:.2f}s (empty is expected)")

    print("[smoke] ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
