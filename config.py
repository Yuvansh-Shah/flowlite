"""Config loader. Reads config.toml from the app folder."""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import tomli


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.toml"


@dataclass
class HotkeyConfig:
    record: str = "ctrl+windows"
    lock_toggle: str = "ctrl+windows+space"
    cancel: str = "esc"
    retrieve_paste: str = "ctrl+alt+v"


@dataclass
class SttConfig:
    model: str = "deepdml/faster-whisper-large-v3-turbo-ct2"
    device: str = "cuda"
    compute_type: str = "int8_float16"
    language: str = "en"
    vad_filter: bool = True


@dataclass
class FormatterConfig:
    enabled: bool = True
    model: str = "gemma3:1b"
    timeout: float = 30.0
    keep_alive: str = "30m"
    ollama_url: str = "http://localhost:11434"


@dataclass
class OutputConfig:
    paste_delay_ms: int = 60
    preserve_clipboard: bool = True
    notify_on_skip: bool = True


@dataclass
class UiConfig:
    show_pill: bool = True
    play_sounds: bool = False


@dataclass
class AudioConfig:
    sample_rate: int = 16000
    channels: int = 1
    max_seconds: int = 600
    min_seconds: float = 0.3


@dataclass
class Config:
    hotkeys: HotkeyConfig = field(default_factory=HotkeyConfig)
    stt: SttConfig = field(default_factory=SttConfig)
    formatter: FormatterConfig = field(default_factory=FormatterConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)


def load(path: Path = CONFIG_PATH) -> Config:
    if not path.exists():
        print(f"[config] {path} not found, using defaults", file=sys.stderr)
        return Config()
    with path.open("rb") as f:
        raw = tomli.load(f)
    return Config(
        hotkeys=HotkeyConfig(**raw.get("hotkeys", {})),
        stt=SttConfig(**raw.get("stt", {})),
        formatter=FormatterConfig(**raw.get("formatter", {})),
        output=OutputConfig(**raw.get("output", {})),
        ui=UiConfig(**raw.get("ui", {})),
        audio=AudioConfig(**raw.get("audio", {})),
    )
