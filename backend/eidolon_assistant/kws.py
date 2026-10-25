"""Local wake-word / interrupt-word spotting with sherpa-onnx.

One zipformer KWS model carries every phrase the assistant listens for; the state
machine decides which labels matter right now (wake word while idle, interrupt
words while a task runs, yes/no while a permission question is up). Detection is
CPU-only and never touches a cloud STT.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import sherpa_onnx

from .config import WakeSettings
from .pinyin import PinyinError, keyword_line, load_tokens

MODEL_DIR = "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"


@dataclass(frozen=True)
class KwsModel:
    dir: Path

    @property
    def encoder(self) -> Path:
        return self.dir / "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"

    @property
    def decoder(self) -> Path:
        return self.dir / "decoder-epoch-12-avg-2-chunk-16-left-64.onnx"

    @property
    def joiner(self) -> Path:
        return self.dir / "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"

    @property
    def tokens(self) -> Path:
        return self.dir / "tokens.txt"

    def ready(self) -> bool:
        return all(p.exists() for p in (self.encoder, self.decoder, self.joiner, self.tokens))


class WakeSpotter:
    """Thin wrapper around ``sherpa_onnx.KeywordSpotter`` that reports labels."""

    def __init__(self, model: KwsModel, labels: dict[str, str], settings: WakeSettings,
                 keywords_file: Path) -> None:
        """``labels`` maps a returned label to the phrase, e.g. ``wake|拉姆`` -> 拉姆."""
        if not model.ready():
            raise FileNotFoundError(f"KWS model files missing under {model.dir}")
        self.model = model
        self.labels = labels
        self.settings = settings
        self.keywords_file = keywords_file
        self._lock = threading.Lock()
        self._write_keywords()
        self._spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(model.tokens),
            encoder=str(model.encoder),
            decoder=str(model.decoder),
            joiner=str(model.joiner),
            keywords_file=str(keywords_file),
            num_threads=settings.threads,
            keywords_score=settings.score,
            keywords_threshold=settings.threshold,
            max_active_paths=4,
        )
        self._stream = self._spotter.create_stream()

    def _write_keywords(self) -> None:
        tokens = load_tokens(self.model.tokens)
        lines: list[str] = []
        for label in self.labels:
            _, phrase = label.split("|", 1)
            try:
                lines.append(keyword_line(phrase, tokens).replace(f"@{phrase}", f"@{label}"))
            except PinyinError:
                raise
        self.keywords_file.parent.mkdir(parents=True, exist_ok=True)
        self.keywords_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def feed(self, samples: np.ndarray, sample_rate: int = 16000) -> list[str]:
        found: list[str] = []
        with self._lock:
            self._stream.accept_waveform(sample_rate, samples.astype(np.float32))
            while self._spotter.is_ready(self._stream):
                self._spotter.decode_stream(self._stream)
                result = self._spotter.get_result(self._stream)
                if result:
                    found.append(result)
                    self._spotter.reset_stream(self._stream)
        return found

    def reset(self) -> None:
        with self._lock:
            self._spotter.reset_stream(self._stream)
