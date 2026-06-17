"""Post-process raw Whisper output: light cleanup, keep EVERY word.

Primary path: Ollama (gemma3:1b by default).
Fallback path: regex cleanup if Ollama is unreachable/slow OR if the LLM
drops too many words (the length guard).

History note: the old prompt's examples literally showed condensing
("first we need budget" -> "Budget"), which trained the small model to
summarise and drop ~half the words. New prompt forbids it, examples keep
the same words in and out, and a length guard rejects any LLM output that
shrank too much.
"""
from __future__ import annotations

import re
import sys
from typing import Optional

import requests

from config import FormatterConfig


SYSTEM_PROMPT = """You are a transcript cleaner. You fix punctuation and \
capitalization and remove ONLY filler sounds. You must keep every real word.

HARD RULES:
- Keep EVERY word the speaker said. Do NOT summarize. Do NOT shorten. Do NOT \
paraphrase. Do NOT drop sentences. The output must have essentially the same \
word count as the input.
- Only remove pure filler: "um", "uh", "er", "hmm". Keep everything else.
- Add capital letters and punctuation (. , ? !).
- If the speaker clearly lists items ("first... second... third", "number one... \
number two", "and also... another thing"), format as a markdown list BUT keep \
all the words of each item.
- Keep names, technical terms, numbers, and URLs exactly.
- Output ONLY the cleaned text. No preamble. No quotes. No commentary.

EXAMPLES (notice: no words are removed except um/uh):

Input: um so basically i think we should ship the feature on friday because the \
tests are passing and the team is ready
Output: So basically I think we should ship the feature on Friday, because the \
tests are passing and the team is ready.

Input: number one we need to fix the login bug number two we need to update the \
docs number three we need to deploy to staging before friday
Output:
1. We need to fix the login bug.
2. We need to update the docs.
3. We need to deploy to staging before Friday.

Input: hey can you uh send me that document we were talking about yesterday i \
need it for the meeting at three
Output: Hey, can you send me that document we were talking about yesterday? I \
need it for the meeting at three."""


class Formatter:
    def __init__(self, cfg: FormatterConfig, dictionary=None):
        self.cfg = cfg
        self.dictionary = dictionary  # optional Dictionary instance

    def format(self, text: str) -> str:
        if not text:
            return text
        # Apply user pronunciation corrections FIRST, on raw text, so the
        # LLM never sees the wrong word.
        if self.dictionary is not None:
            text = self.dictionary.apply_corrections(text)

        if not self.cfg.enabled:
            return _safe_fallback(text)

        cleaned = self._llm_clean(text)
        if cleaned is None:
            return _safe_fallback(text)

        # LENGTH GUARD: if the LLM dropped too many words, it summarised.
        # Reject it and use the safe regex cleanup, which never drops words.
        in_words = _word_count(text)
        out_words = _word_count(cleaned)
        if in_words >= 6 and out_words < in_words * 0.75:
            print(
                f"[formatter] LLM shrank {in_words}->{out_words} words "
                f"(>25% lost) — rejecting, using safe cleanup",
                file=sys.stderr,
            )
            return _safe_fallback(text)

        # Corrections again on the cleaned text (LLM may have re-introduced).
        if self.dictionary is not None:
            cleaned = self.dictionary.apply_corrections(cleaned)
        return cleaned

    def warmup(self) -> bool:
        if not self.cfg.enabled:
            return True
        url = f"{self.cfg.ollama_url.rstrip('/')}/api/generate"
        payload = {
            "model": self.cfg.model,
            "prompt": "ok",
            "stream": False,
            "keep_alive": self.cfg.keep_alive,
            "options": {"num_predict": 1, "temperature": 0.0},
        }
        try:
            r = requests.post(url, json=payload, timeout=60)
            r.raise_for_status()
            return True
        except Exception as e:
            print(f"[formatter] warmup failed: {e}", file=sys.stderr)
            return False

    def _llm_clean(self, text: str) -> Optional[str]:
        url = f"{self.cfg.ollama_url.rstrip('/')}/api/generate"
        # Scale output budget to input length so long dictations are never
        # truncated. ~2 tokens/word + generous headroom.
        n_predict = max(512, _word_count(text) * 4 + 256)
        payload = {
            "model": self.cfg.model,
            "system": SYSTEM_PROMPT,
            "prompt": f"Input: {text}\nOutput:",
            "stream": False,
            "keep_alive": self.cfg.keep_alive,
            "options": {
                "temperature": 0.0,
                "num_predict": n_predict,
                "top_p": 0.9,
            },
        }
        try:
            r = requests.post(url, json=payload, timeout=self.cfg.timeout)
            r.raise_for_status()
            response = r.json().get("response", "").strip()
            response = _strip_wrapping_quotes(response)
            return response or None
        except Exception as e:
            print(f"[formatter] LLM failed: {e}, using rules", file=sys.stderr)
            return None


def _word_count(s: str) -> int:
    return len(re.findall(r"\b\w+\b", s))


def _strip_wrapping_quotes(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1].strip()
    return s


def _basic_cleanup(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# Only true filler sounds. Keep "like", "you know", etc. — removing those was
# also eating meaning. Better to leave a word than delete a real one.
_FILLERS = re.compile(r"\b(um+|uh+|erm+|hmm+)\b[,]?\s*", re.IGNORECASE)


def _safe_fallback(text: str) -> str:
    text = _basic_cleanup(text)
    text = _FILLERS.sub("", text)
    text = re.sub(r"\s+([.,!?;:])", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = _capitalize_sentences(text)
    return text


def _capitalize_sentences(text: str) -> str:
    if not text:
        return text
    parts = re.split(r"([.!?]\s+)", text)
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 0 and part:
            part = part[0].upper() + part[1:]
        out.append(part)
    result = "".join(out)
    if result and result[-1] not in ".!?":
        result += "."
    return result
