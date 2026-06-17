"""User dictionary — custom vocabulary + pronunciation corrections.

Two jobs:
  1. Bias Whisper toward the user's words by injecting them into the
     transcription `initial_prompt`. Whisper genuinely uses this to prefer
     spellings/names it would otherwise mangle.
  2. Post-correct the transcript: fix recurring mis-hearings where one word
     sounds like another in this user's accent.

File format (dictionary.txt, hand-editable, UTF-8):

    # lines starting with # are comments
    # bare word -> just biases Whisper toward it
    Yuvansh
    Dhirubhai
    flowlite

    # "heard => meant" -> replaces wrong hearing with intended word
    yuvanshhh => Yuvansh
    fluvlite => flowlite
    pie thon => python

Matching for corrections is case-insensitive, whole-word.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DICT_PATH = APP_DIR / "dictionary.txt"


class Dictionary:
    def __init__(self, path: Path = DICT_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._terms: list[str] = []                 # bias words
        self._corrections: list[tuple[str, str]] = []  # (heard, meant)
        self.reload()

    def reload(self) -> None:
        terms: list[str] = []
        corrections: list[tuple[str, str]] = []
        if self.path.exists():
            for raw in self.path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=>" in line:
                    heard, meant = line.split("=>", 1)
                    heard, meant = heard.strip(), meant.strip()
                    if heard and meant:
                        corrections.append((heard, meant))
                        # the intended word is also a bias term
                        terms.append(meant)
                else:
                    terms.append(line)
        with self._lock:
            # dedupe, keep order
            seen = set()
            self._terms = [t for t in terms if not (t.lower() in seen or seen.add(t.lower()))]
            self._corrections = corrections

    @property
    def terms(self) -> list[str]:
        with self._lock:
            return list(self._terms)

    def initial_prompt(self) -> str:
        """A short hint string for Whisper. Keep it natural — a comma list
        of names/words primes the model without derailing it."""
        with self._lock:
            if not self._terms:
                return ""
            # Cap length so we don't blow the prompt budget.
            words = self._terms[:60]
        return "Vocabulary: " + ", ".join(words) + "."

    def apply_corrections(self, text: str) -> str:
        with self._lock:
            corrections = list(self._corrections)
        for heard, meant in corrections:
            # whole-word, case-insensitive, allow internal spaces in `heard`
            pattern = r"\b" + re.escape(heard) + r"\b"
            text = re.sub(pattern, meant, text, flags=re.IGNORECASE)
        return text

    def add_term(self, term: str) -> None:
        term = term.strip()
        if not term:
            return
        with self._lock:
            if term.lower() in {t.lower() for t in self._terms}:
                return
            self._terms.append(term)
        self._append_line(term)

    def add_correction(self, heard: str, meant: str) -> None:
        heard, meant = heard.strip(), meant.strip()
        if not heard or not meant:
            return
        with self._lock:
            self._corrections.append((heard, meant))
        self._append_line(f"{heard} => {meant}")

    def _append_line(self, line: str) -> None:
        # ensure file ends with newline then append
        existing = ""
        if self.path.exists():
            existing = self.path.read_text(encoding="utf-8")
        if existing and not existing.endswith("\n"):
            existing += "\n"
        self.path.write_text(existing + line + "\n", encoding="utf-8")
