"""Microphone capture into an in-memory numpy buffer.

Supports pause/resume — the audio stream stops but the buffer is kept, so a
subsequent resume continues the same recording session."""
from __future__ import annotations

import threading
from typing import Optional

import numpy as np
import sounddevice as sd

from config import AudioConfig


class Recorder:
    def __init__(self, cfg: AudioConfig):
        self.cfg = cfg
        self._chunks: list[np.ndarray] = []
        self._stream: Optional[sd.InputStream] = None
        self._lock = threading.Lock()
        self._max_samples = cfg.max_seconds * cfg.sample_rate
        self._active = False  # True between start() and stop()/discard()
        self._paused = False

    def _callback(self, indata, frames, time_info, status):
        if status:
            return
        with self._lock:
            self._chunks.append(indata.copy())

    def _open_stream(self) -> None:
        self._stream = sd.InputStream(
            samplerate=self.cfg.sample_rate,
            channels=self.cfg.channels,
            dtype="float32",
            callback=self._callback,
        )
        self._stream.start()

    def _close_stream(self) -> None:
        if self._stream is None:
            return
        try:
            self._stream.stop()
            self._stream.close()
        finally:
            self._stream = None

    def start(self) -> None:
        """Begin a fresh recording session."""
        with self._lock:
            self._chunks.clear()
        self._active = True
        self._paused = False
        self._open_stream()

    def pause(self) -> None:
        """Stop capturing audio but keep the buffer for resume()."""
        if not self._active or self._paused:
            return
        self._close_stream()
        self._paused = True

    def resume(self) -> None:
        """Continue capturing into the existing buffer."""
        if not self._active or not self._paused:
            return
        self._paused = False
        self._open_stream()

    def stop(self) -> np.ndarray:
        """End the session and return the recorded audio (mono float32)."""
        if not self._active:
            return np.zeros(0, dtype=np.float32)
        self._close_stream()
        self._active = False
        self._paused = False
        with self._lock:
            if not self._chunks:
                return np.zeros(0, dtype=np.float32)
            audio = np.concatenate(self._chunks, axis=0).flatten()
            self._chunks.clear()
        if len(audio) > self._max_samples:
            audio = audio[: self._max_samples]
        return audio

    def discard(self) -> None:
        """Cancel an in-progress session without returning audio."""
        self._close_stream()
        self._active = False
        self._paused = False
        with self._lock:
            self._chunks.clear()

    @property
    def elapsed_seconds(self) -> float:
        with self._lock:
            total = sum(c.shape[0] for c in self._chunks)
        return total / self.cfg.sample_rate

    @property
    def is_recording(self) -> bool:
        return self._active

    @property
    def is_paused(self) -> bool:
        return self._paused

    def current_level(self) -> float:
        """RMS of the most recent chunk, scaled into a perceptual 0..1 range.
        Used to drive the waveform animation in the UI."""
        with self._lock:
            if not self._chunks:
                return 0.0
            recent = self._chunks[-1]
        if recent.size == 0:
            return 0.0
        rms = float(np.sqrt(np.mean(recent.astype(np.float32) ** 2)))
        # Speech RMS is typically 0.01-0.2 — scale so quiet talking shows
        # visible movement and loud speech saturates.
        return min(1.0, rms * 6.0)
