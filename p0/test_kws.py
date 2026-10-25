#!/usr/bin/env python3
"""P0-B: local Chinese wake-word detection with sherpa-onnx KWS (zipformer, pinyin).

Modes
  --wav FILE      feed a 16 kHz mono wav file through the spotter as if live
  --live SECONDS  capture the default microphone and print detections
  --silence SEC   capture only, report false positives

The keyword list is generated from the character config's wake words through a
small pinyin table, so the same table seeds the real KWS service later.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np
import sherpa_onnx
import sounddevice as sd

ROOT = Path(__file__).resolve().parent
MODEL = ROOT.parent / "models" / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
SAMPLE_RATE = 16000
CHUNK = 1600  # 100 ms

# 拉姆 / 拉姆蕾萨尔 -> pinyin tokens used by the wenetspeech KWS model
PINYIN_KEYWORDS = {
    "拉姆": "l ā m ǔ",
    "拉姆蕾萨尔": "l ā m ǔ l ěi s à ěr",
}


def write_keywords(path: Path, words: list[str]) -> None:
    lines = []
    for word in words:
        pinyin = PINYIN_KEYWORDS.get(word)
        if pinyin is None:
            raise SystemExit(f"no pinyin mapping for {word!r}; add it to PINYIN_KEYWORDS")
        lines.append(f"{pinyin} @{word}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"keywords file -> {path}")
    print(path.read_text(encoding="utf-8"))


def build_spotter(keywords_file: Path, threads: int = 2, threshold: float = 0.25,
                  score: float = 1.5) -> sherpa_onnx.KeywordSpotter:
    return sherpa_onnx.KeywordSpotter(
        encoder=str(MODEL / "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
        decoder=str(MODEL / "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
        joiner=str(MODEL / "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
        tokens=str(MODEL / "tokens.txt"),
        keywords_file=str(keywords_file),
        num_threads=threads,
        keywords_score=score,
        keywords_threshold=threshold,
        max_active_paths=4,
    )


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != SAMPLE_RATE:
            raise SystemExit(
                f"expected 16k mono 16-bit wav, got {w.getnchannels()}ch "
                f"{w.getsampwidth() * 8}bit {w.getframerate()}Hz"
            )
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def run_wav(spotter: sherpa_onnx.KeywordSpotter, path: Path, labels: dict[str, int]) -> dict:
    audio = read_wav(path)
    # trailing silence so the streaming decoder can commit the last keyword,
    # exactly like the natural pause after someone says the wake word
    audio = np.concatenate([audio, np.zeros(SAMPLE_RATE, dtype=np.float32)])
    stream = spotter.create_stream()
    detected: list[dict] = []
    for i in range(0, len(audio), CHUNK):
        chunk = audio[i:i + CHUNK]
        stream.accept_waveform(SAMPLE_RATE, chunk)
        while spotter.is_ready(stream):
            spotter.decode_stream(stream)
            result = spotter.get_result(stream)
            if result:
                break
        else:
            result = ""
        if result:
            fed = min(i + CHUNK, len(audio))
            detected.append({
                "keyword": result,
                "audio_seconds_at_detection": round(fed / SAMPLE_RATE, 3),
                "latency_after_audio_end": round((fed - len(audio)) / SAMPLE_RATE, 3),
            })
            labels[result] = labels.get(result, 0) + 1
            spotter.reset_stream(stream)
    return {
        "file": str(path),
        "audio_seconds": round(len(audio) / SAMPLE_RATE, 3),
        "detections": detected,
    }


def run_live(spotter: sherpa_onnx.KeywordSpotter, seconds: float, labels: dict[str, int],
             play_reply: str | None = None) -> dict:
    stream = spotter.create_stream()
    detections: list[dict] = []
    t_start = time.time()
    cpu_start = time.process_time()
    print(f"\n>>> listening on default input for {seconds:.0f}s — please say 拉姆", flush=True)
    if play_reply:
        # play the wake reply immediately to test self-wake, as the real app will
        subprocess.Popen(["say", "-v", "Tingting", play_reply])
        print(f">>> playing TTS reply {play_reply!r} through the speakers now (self-wake test)", flush=True)
    else:
        subprocess.Popen(["say", "-v", "Tingting", "开始"])
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        blocksize=CHUNK, device=None) as mic:
        while time.time() - t_start < seconds:
            block, overflowed = mic.read(CHUNK)
            samples = block.reshape(-1).astype(np.float32)
            stream.accept_waveform(SAMPLE_RATE, samples)
            result = ""
            while spotter.is_ready(stream):
                spotter.decode_stream(stream)
                result = spotter.get_result(stream)
                if result:
                    break
            now = time.time() - t_start
            if result:
                detections.append({"keyword": result, "at_seconds": round(now, 3)})
                labels[result] = labels.get(result, 0) + 1
                print(f"  [WAKE] {result!r} at {now:.2f}s", flush=True)
                spotter.reset_stream(stream)
    cpu = time.process_time() - cpu_start
    return {
        "mode": "live",
        "seconds": seconds,
        "detections": detections,
        "cpu_seconds": round(cpu, 3),
        "cpu_percent_of_one_core": round(100 * cpu / seconds, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", type=Path)
    ap.add_argument("--live", type=float)
    ap.add_argument("--self-wake", action="store_true", help="play 我在。 while listening")
    ap.add_argument("--threshold", type=float, default=0.25)
    ap.add_argument("--score", type=float, default=1.5)
    ap.add_argument("--threads", type=int, default=1)
    ap.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args()

    kw_file = ROOT / "out" / "kws_keywords.txt"
    kw_file.parent.mkdir(exist_ok=True)
    write_keywords(kw_file, ["拉姆", "拉姆蕾萨尔"])

    print("loading model ...", flush=True)
    t0 = time.time()
    spotter = build_spotter(kw_file, threads=args.threads, threshold=args.threshold, score=args.score)
    print(f"ready in {time.time() - t0:.2f}s", flush=True)

    labels: dict[str, int] = {}
    report: dict = {"threshold": args.threshold, "score": args.score, "threads": args.threads}
    if args.wav:
        runs = [run_wav(spotter, args.wav, labels) for _ in range(args.repeat)]
        report["wav"] = runs
    if args.live:
        report["live"] = run_live(spotter, args.live, labels, "我在。" if args.self_wake else None)
    report["counts"] = labels
    out = ROOT / "out" / "kws_report.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
