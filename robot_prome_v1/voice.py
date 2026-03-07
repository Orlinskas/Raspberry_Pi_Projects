#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# Author:   Vlad Orlinskas
# Site:     https://prometeriy.com
# Project:  robot_prome_v1 — LLM-driven autonomous robot experiment
# License:  Free for any use
# ---------------------------------------------------------------------------

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Optional, Union

from settings import read_json

LOGGER = logging.getLogger("voice")

# Language code for espeak: "ru", "en", etc. Empty = default.
VOICE_LANG = "ru"
# Words per minute for espeak; slower often clearer for Russian (default ~160).
VOICE_SPEED_WPM = 120
# Engine: "auto" (try piper if model set, else espeak-ng, else espeak), "espeak", "espeak-ng", "piper".
VOICE_ENGINE = "auto"
# Path to Piper .onnx model for Russian (e.g. ru_RU-irina-medium). If set and piper installed, used when VOICE_ENGINE is "auto" or "piper". Override with env VOICE_PIPER_MODEL.
VOICE_PIPER_MODEL_PATH = ""  # e.g. "/home/pi/piper/ru_RU-irina-medium.onnx"
VOICE_PIPER_MODEL = (os.environ.get("VOICE_PIPER_MODEL") or VOICE_PIPER_MODEL_PATH or "").strip() or None
VOICE_MAX_LENGTH = 300
VOICE_TIMEOUT_S = 30.0
_ESPEAK_WARNED = False
_PIPER_WARNED = False


def _sanitize_phrase(text: str) -> str:
    if not text or not isinstance(text, str):
        return ""
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", text.strip())
    return s[:VOICE_MAX_LENGTH].strip()


def _espeak_binary() -> Optional[str]:
    """Prefer espeak-ng (better Russian), fallback to espeak."""
    for name in ("espeak-ng", "espeak"):
        if shutil.which(name):
            return name
    return None


def _piper_available() -> bool:
    return bool(shutil.which("piper") and VOICE_PIPER_MODEL and Path(VOICE_PIPER_MODEL).exists())


def _play_phrase_espeak(phrase: str) -> bool:
    global _ESPEAK_WARNED
    binary = _espeak_binary()
    if not binary:
        if not _ESPEAK_WARNED:
            LOGGER.warning("espeak not found; install: apt install espeak-ng")
            _ESPEAK_WARNED = True
        return False
    lang = (os.environ.get("VOICE_LANG") or VOICE_LANG or "").strip()
    cmd = [binary, "-a", "200", "-s", str(VOICE_SPEED_WPM), phrase]
    if lang:
        cmd = [binary, "-v", lang, "-a", "200", "-s", str(VOICE_SPEED_WPM), phrase]
    try:
        subprocess.run(cmd, timeout=VOICE_TIMEOUT_S, capture_output=True, check=False)
        return True
    except FileNotFoundError:
        if not _ESPEAK_WARNED:
            LOGGER.warning("espeak not found; install: apt install espeak-ng")
            _ESPEAK_WARNED = True
        return False
    except subprocess.TimeoutExpired:
        LOGGER.warning("espeak timed out (truncated)")
        return False
    except OSError as exc:
        LOGGER.warning("espeak failed: %s", exc)
        return False


def _play_phrase_piper(phrase: str) -> bool:
    global _PIPER_WARNED
    if not _piper_available():
        if not _PIPER_WARNED:
            LOGGER.warning("Piper not available: install piper and set VOICE_PIPER_MODEL to .onnx path")
            _PIPER_WARNED = True
        return False
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            wav_path = f.name
        try:
            proc = subprocess.run(
                ["piper", "--model", VOICE_PIPER_MODEL, "--output_file", wav_path],
                input=phrase.encode("utf-8"),
                capture_output=True,
                timeout=VOICE_TIMEOUT_S,
                cwd=os.path.dirname(VOICE_PIPER_MODEL) or ".",
            )
            if proc.returncode != 0:
                LOGGER.warning("piper failed: %s", proc.stderr.decode("utf-8", errors="replace")[:200])
                return False
            if not os.path.exists(wav_path) or os.path.getsize(wav_path) == 0:
                return False
            subprocess.run(["aplay", "-q", wav_path], timeout=VOICE_TIMEOUT_S, check=False, capture_output=True)
        finally:
            if os.path.exists(wav_path):
                os.unlink(wav_path)
        return True
    except FileNotFoundError:
        if not _PIPER_WARNED:
            LOGGER.warning("piper or aplay not found; install piper and alsa-utils")
            _PIPER_WARNED = True
        return False
    except subprocess.TimeoutExpired:
        LOGGER.warning("piper timed out")
        return False
    except OSError as exc:
        LOGGER.warning("piper failed: %s", exc)
        return False


def play_phrase(text: str) -> None:
    phrase = _sanitize_phrase(text)
    if not phrase:
        return
    engine = (os.environ.get("VOICE_ENGINE") or VOICE_ENGINE or "auto").strip().lower()
    if engine == "piper" or (engine == "auto" and _piper_available()):
        if _play_phrase_piper(phrase):
            return
        if engine == "piper":
            return
    _play_phrase_espeak(phrase)


def run_voice_loop(
    command_path: Union[Path, str],
    poll_interval_s: float = 0.05,
    stop_event: Optional[threading.Event] = None,
) -> None:
    stop_event = stop_event or threading.Event()
    command_path = Path(command_path)
    last_command_id = ""

    LOGGER.info("Voice started command_path=%s", command_path)
    while not stop_event.is_set():
        raw = read_json(command_path)
        if not isinstance(raw, dict):
            stop_event.wait(poll_interval_s)
            continue
        command_id = str(raw.get("command_id", ""))
        if command_id != last_command_id:
            last_command_id = command_id
            voice_raw = raw.get("voice")
            voice = (str(voice_raw).strip() if voice_raw is not None else "") or None
            if voice:
                LOGGER.info("Voice playing: %s", voice[:80] + ("..." if len(voice) > 80 else ""))
                play_phrase(voice)
        stop_event.wait(poll_interval_s)
    LOGGER.info("Voice stopped")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Voice module: play phrases via espeak")
    parser.add_argument(
        "--test",
        nargs="?",
        const="Привет, я робот, тест звука",
        default=None,
        metavar="PHRASE",
        help="Test run: play a phrase and exit. With no argument uses default phrase. Running 'voice.py' with no options is test mode.",
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run voice loop (watch command.json) instead of test mode.",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    if args.loop:
        from settings import COMMAND_PATH
        run_voice_loop(COMMAND_PATH, stop_event=threading.Event())
        return
    phrase = args.test if args.test is not None else "Привет, я робот, тест звука"
    LOGGER.info("Test mode: playing phrase")
    play_phrase(phrase)


if __name__ == "__main__":
    main()
