"""faster-whisper wrapper. Loads once, transcribes many times."""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional


def _add_nvidia_dll_paths() -> None:
    """faster-whisper / ctranslate2 needs cuBLAS, cuDNN, and cuda_nvrtc DLLs on
    the DLL search path. The pip wheels (nvidia-cublas-cu12, nvidia-cudnn-cu12)
    drop them under site-packages/nvidia/*/bin. We register every nvidia/*/bin
    we find via three mechanisms (add_dll_directory + PATH prepend + explicit
    preload) because ctranslate2's native loader doesn't honour them all.
    """
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        import site

        bases = [Path(p) for p in site.getsitepackages()]
        if site.USER_SITE:
            bases.append(Path(site.USER_SITE))

        bin_dirs: list[Path] = []
        for base in bases:
            nv = base / "nvidia"
            if not nv.is_dir():
                continue
            for sub in nv.iterdir():
                bd = sub / "bin"
                if bd.is_dir():
                    bin_dirs.append(bd)

        for d in bin_dirs:
            try:
                os.add_dll_directory(str(d))
            except Exception:
                pass
        if bin_dirs:
            extra = os.pathsep.join(str(d) for d in bin_dirs)
            os.environ["PATH"] = extra + os.pathsep + os.environ.get("PATH", "")

        # Preload the libraries ctranslate2 will look for, in dependency order.
        # Once they're loaded by name, the OS will satisfy the dependency lookup
        # from ctranslate2's native DLL without searching the filesystem again.
        for dll_name in ("cublasLt64_12.dll", "cublas64_12.dll", "cudnn64_9.dll"):
            try:
                ctypes.WinDLL(dll_name)
            except OSError:
                pass
    except Exception as e:
        print(f"[stt] DLL path injection skipped: {e}", file=sys.stderr)


_add_nvidia_dll_paths()

import numpy as np
from faster_whisper import WhisperModel

from config import SttConfig


class Transcriber:
    def __init__(self, cfg: SttConfig):
        self.cfg = cfg
        self._model: Optional[WhisperModel] = None
        # Guards model creation AND inference. CTranslate2 model load and a
        # concurrent transcribe from two threads (warmup thread + worker
        # thread) caused a CUDA access-violation crash. This serializes both.
        self._lock = threading.Lock()

    def load(self) -> None:
        # Double-checked locking so two threads can't build the model twice.
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            self._load_locked()

    def _load_locked(self) -> None:
        t0 = time.time()
        print(
            f"[stt] loading {self.cfg.model} on {self.cfg.device} "
            f"({self.cfg.compute_type})...",
            file=sys.stderr,
        )
        try:
            self._model = WhisperModel(
                self.cfg.model,
                device=self.cfg.device,
                compute_type=self.cfg.compute_type,
            )
        except Exception as e:
            # GPU load failed (no CUDA libs, OOM, etc.) — fall back to CPU.
            print(
                f"[stt] GPU load failed ({e}), falling back to CPU int8",
                file=sys.stderr,
            )
            self._model = WhisperModel(
                self.cfg.model,
                device="cpu",
                compute_type="int8",
            )
        print(f"[stt] ready in {time.time() - t0:.1f}s", file=sys.stderr)

    def transcribe(self, audio: np.ndarray, initial_prompt: str = "") -> str:
        if audio.size == 0:
            return ""
        self.load()
        language = self.cfg.language or None

        vad_params = dict(
            min_silence_duration_ms=700,
            speech_pad_ms=400,
            threshold=0.30,
        )
        # Hold the lock during inference too: the CUDA model is NOT safe to
        # call from two threads at once (caused the access-violation crash).
        with self._lock:
            assert self._model is not None
            segments, _info = self._model.transcribe(
                audio,
                language=language,
                vad_filter=self.cfg.vad_filter,
                vad_parameters=vad_params if self.cfg.vad_filter else None,
                beam_size=5,
                best_of=5,
                condition_on_previous_text=True,
                initial_prompt=initial_prompt or None,
                no_speech_threshold=0.85,
                temperature=0.0,
            )
            # Materialise the generator INSIDE the lock — segments are lazy
            # and pull on the GPU as you iterate.
            return " ".join(s.text.strip() for s in segments).strip()
