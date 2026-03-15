#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# Author:   Vlad Orlinskas
# Site:     https://prometeriy.com
# Project:  robot_prome_v1 — LLM-driven autonomous robot experiment
# License:  Free for any use
# ---------------------------------------------------------------------------

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Optional

from settings import (
    MicrophoneConfig,
    VOICE_MUTE_EVENT,
    atomic_write_json,
    read_json,
    zero_state_payload,
)
from voice import play_phrase

LOGGER = logging.getLogger("microphone")

try:
    import sounddevice as sd
except ImportError:
    sd = None

try:
    from vosk import KaldiRecognizer, Model, SetLogLevel
except ImportError:
    KaldiRecognizer = None
    Model = None
    SetLogLevel = None


def _normalize_text(text: str) -> str:
    return " ".join(str(text).strip().split())


def _extract_text(raw_json: str) -> str:
    try:
        payload = json.loads(raw_json)
    except (TypeError, ValueError):
        return ""
    text = payload.get("text", "")
    return _normalize_text(text)


def _extract_partial_text(raw_json: str) -> str:
    try:
        payload = json.loads(raw_json)
    except (TypeError, ValueError):
        return ""
    text = payload.get("partial", "")
    return _normalize_text(text)


def _update_state_command(state_path: Path, command_text: str) -> None:
    state = read_json(state_path)
    if not isinstance(state, dict):
        state = zero_state_payload()
    state["command"] = command_text
    atomic_write_json(state_path, state)


def _chunk_to_bytes(chunk: object) -> bytes:
    # Vosk cffi binding expects raw bytes; RawInputStream may return a buffer-like object.
    if isinstance(chunk, (bytes, bytearray)):
        return bytes(chunk)
    if hasattr(chunk, "tobytes"):
        return chunk.tobytes()
    return bytes(chunk)


def _sample_width_bytes(dtype: str) -> int:
    mapping = {
        "int16": 2,
        "int32": 4,
        "float32": 4,
        "uint8": 1,
    }
    return mapping.get(str(dtype).lower(), 2)


def _venv_hint() -> str:
    in_venv = hasattr(sys, "base_prefix") and sys.prefix != sys.base_prefix
    if in_venv:
        return "Run: pip install -r requirements.txt"
    return "Activate venv first: source .venv/bin/activate && pip install -r requirements.txt"


def _speak_prompt(phrase: str) -> None:
    prompt = str(phrase).strip()
    if not prompt:
        return
    try:
        play_phrase(prompt)
    except Exception as exc:
        LOGGER.warning("Prompt playback failed: %s", exc)


def _log_received_command(source: str, command_text: str) -> None:
    text = _normalize_text(command_text)
    if not text:
        LOGGER.info("[%s] Robot command received: <empty>", source)
        return
    LOGGER.info("[%s] Robot command received: %s", source, text)


class SpeechRecognizer:
    def __init__(self, config: MicrophoneConfig) -> None:
        self.config = config
        self._model = None
        self._frames_per_chunk = max(200, int(self.config.sample_rate * 0.2))
        self._active_sample_rate = int(self.config.sample_rate)

    def initialize(self) -> None:
        if sd is None:
            raise RuntimeError(f"sounddevice not installed. {_venv_hint()}")
        if Model is None or KaldiRecognizer is None:
            raise RuntimeError(f"vosk not installed. {_venv_hint()}")

        model_path = Path(self.config.vosk_model_path).expanduser().resolve()
        if not model_path.exists():
            raise RuntimeError(f"Vosk model not found: {model_path}")

        if SetLogLevel is not None:
            SetLogLevel(-1)

        self._model = Model(str(model_path))
        LOGGER.info("Vosk model loaded: %s", model_path)

    def _new_recognizer(self):
        if self._model is None:
            raise RuntimeError("Speech recognizer model is not initialized")
        recognizer = KaldiRecognizer(self._model, float(self._active_sample_rate))
        recognizer.SetWords(False)
        return recognizer

    def _candidate_sample_rates(self, device) -> list[int]:
        candidates: list[int] = [int(self.config.sample_rate)]
        try:
            info = sd.query_devices(device, "input")
            default_sr_raw = info.get("default_samplerate")
            if default_sr_raw:
                candidates.append(int(round(float(default_sr_raw))))
        except Exception:
            pass
        candidates.extend([48000, 44100, 32000, 22050, 16000, 8000])

        unique: list[int] = []
        seen: set[int] = set()
        for rate in candidates:
            if rate <= 0 or rate in seen:
                continue
            seen.add(rate)
            unique.append(rate)
        return unique

    def _open_stream(self):
        device = None if self.config.device_index < 0 else self.config.device_index
        last_exc = None
        for rate in self._candidate_sample_rates(device):
            try:
                stream = sd.RawInputStream(
                    samplerate=rate,
                    blocksize=max(200, int(rate * 0.2)),
                    device=device,
                    channels=self.config.channels,
                    dtype=self.config.dtype,
                )
                self._active_sample_rate = int(rate)
                self._frames_per_chunk = max(200, int(self._active_sample_rate * 0.2))
                if self._active_sample_rate != int(self.config.sample_rate):
                    LOGGER.warning(
                        "Sample rate %s unsupported by device, using %s",
                        self.config.sample_rate,
                        self._active_sample_rate,
                    )
                return stream
            except Exception as exc:
                last_exc = exc
                continue

        raise RuntimeError(
            f"Unable to open microphone stream for device={device}. "
            f"Tried sample rates: {self._candidate_sample_rates(device)}. Last error: {last_exc}"
        )

    def wait_wake_word(self, stream, stop_event: threading.Event) -> bool:
        recognizer = self._new_recognizer()
        wake_word = _normalize_text(self.config.wake_word).lower()
        deadline = time.monotonic() + self.config.wake_window_s

        while not stop_event.is_set() and time.monotonic() < deadline:
            data, overflowed = stream.read(self._frames_per_chunk)
            pcm = _chunk_to_bytes(data)
            if overflowed:
                LOGGER.debug("Audio overflow while waiting wake word")

            if recognizer.AcceptWaveform(pcm):
                full_text = _extract_text(recognizer.Result()).lower()
                if full_text:
                    LOGGER.info("Wake window full text: %s", full_text)
                if full_text and wake_word in full_text:
                    LOGGER.info("Wake word detected: %s", self.config.wake_word)
                    return True
            else:
                partial_text = _extract_partial_text(recognizer.PartialResult()).lower()
                if partial_text:
                    LOGGER.info("Wake window partial text: %s", partial_text)
                if self.config.log_partial_results and partial_text:
                    LOGGER.debug("Wake partial: %s", partial_text)
                if partial_text and wake_word in partial_text:
                    LOGGER.info("Wake word detected: %s", self.config.wake_word)
                    return True

        return False

    def record_command(self, stream, stop_event: threading.Event) -> str:
        recognizer = self._new_recognizer()
        texts: list[str] = []
        deadline = time.monotonic() + self.config.command_record_s

        while not stop_event.is_set() and time.monotonic() < deadline:
            data, overflowed = stream.read(self._frames_per_chunk)
            pcm = _chunk_to_bytes(data)
            if overflowed:
                LOGGER.debug("Audio overflow while recording command")
            if recognizer.AcceptWaveform(pcm):
                full_text = _extract_text(recognizer.Result())
                if full_text:
                    texts.append(full_text)
            elif self.config.log_partial_results:
                partial_text = _extract_partial_text(recognizer.PartialResult())
                if partial_text:
                    LOGGER.debug("Command partial: %s", partial_text)

        tail_text = _extract_text(recognizer.FinalResult())
        if tail_text:
            texts.append(tail_text)

        return _normalize_text(" ".join(texts))

    def run_loop(self, stop_event: Optional[threading.Event] = None) -> None:
        stop_event = stop_event or threading.Event()
        self.initialize()
        LOGGER.info("Microphone started state_path=%s", self.config.state_path)

        with self._open_stream() as stream:
            while not stop_event.is_set():
                if not self.wait_wake_word(stream, stop_event):
                    stop_event.wait(self.config.poll_interval_s)
                    continue

                self.capture_command_once(stream, stop_event)

        LOGGER.info("Microphone stopped")

    def capture_command_once(self, stream, stop_event: threading.Event) -> Optional[str]:
        VOICE_MUTE_EVENT.set()
        try:
            _speak_prompt(self.config.trigger_ack_prompt)
            LOGGER.info("Command recording started (%.1fs)", self.config.command_record_s)
            command_text = self.record_command(stream, stop_event)
            LOGGER.info("Command recording finished")

            if len(command_text) < max(0, self.config.min_command_chars):
                LOGGER.info("Command ignored: too short")
                return None

            _log_received_command("loop", command_text)
            _update_state_command(self.config.state_path, command_text)
            LOGGER.info("State updated with command")
            return command_text
        finally:
            VOICE_MUTE_EVENT.clear()


def run_microphone_loop(config: MicrophoneConfig, stop_event: Optional[threading.Event] = None) -> None:
    stop_event = stop_event or threading.Event()
    while not stop_event.is_set():
        recognizer = SpeechRecognizer(config)
        try:
            recognizer.run_loop(stop_event=stop_event)
            return
        except Exception as exc:
            LOGGER.error("Microphone loop error: %s", exc)
            stop_event.wait(config.retry_delay_s)


def run_test_mode(config: MicrophoneConfig) -> int:
    recognizer = SpeechRecognizer(config)
    recognizer.initialize()

    _speak_prompt(config.test_start_prompt)
    LOGGER.info("Test mode: recording %.1f seconds", config.command_record_s)
    with recognizer._open_stream() as stream:
        text = recognizer.record_command(stream, threading.Event())
    _speak_prompt(config.test_done_stt_prompt)
    _log_received_command("test-stt", text)
    LOGGER.info("Test recognized text: %s", text or "<empty>")
    print(text)
    return 0


def run_test_scenario_mode(config: MicrophoneConfig) -> int:
    recognizer = SpeechRecognizer(config)
    recognizer.initialize()
    LOGGER.info("Scenario test: waiting wake word '%s'", config.wake_word)

    with recognizer._open_stream() as stream:
        stop_event = threading.Event()
        while True:
            if recognizer.wait_wake_word(stream, stop_event):
                command_text = recognizer.capture_command_once(stream, stop_event)
                _log_received_command("test-scenario", command_text or "")
                LOGGER.info("Scenario test completed")
                print(command_text or "")
                return 0 if command_text else 1
            stop_event.wait(config.poll_interval_s)


def run_test_audio_mode(config: MicrophoneConfig) -> int:
    if sd is None:
        raise RuntimeError(f"sounddevice not installed. {_venv_hint()}")
    if not shutil.which("aplay"):
        raise RuntimeError("aplay not found. Install with: sudo apt install alsa-utils")

    recorder = SpeechRecognizer(config)
    chunks: list[bytes] = []
    _speak_prompt(config.test_start_prompt)
    LOGGER.info("Audio test mode: recording %.1f seconds", config.command_record_s)
    with recorder._open_stream() as stream:
        deadline = time.monotonic() + config.command_record_s
        while time.monotonic() < deadline:
            data, overflowed = stream.read(recorder._frames_per_chunk)
            if overflowed:
                LOGGER.debug("Audio overflow in test audio mode")
            chunks.append(_chunk_to_bytes(data))

    pcm_bytes = b"".join(chunks)
    if not pcm_bytes:
        LOGGER.warning("Audio test mode: empty recording")
        return 1

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
        wav_path = handle.name
    try:
        with wave.open(wav_path, "wb") as wav_file:
            wav_file.setnchannels(max(1, int(config.channels)))
            wav_file.setsampwidth(_sample_width_bytes(config.dtype))
            wav_file.setframerate(int(config.sample_rate))
            wav_file.writeframes(pcm_bytes)

        _speak_prompt(config.test_done_audio_prompt)
        LOGGER.info("Audio test mode: playing recorded audio")
        subprocess.run(
            ["aplay", "-q", wav_path],
            timeout=float(config.test_audio_play_timeout_s),
            check=False,
            capture_output=True,
        )
    finally:
        if os.path.exists(wav_path):
            os.unlink(wav_path)

    LOGGER.info("Audio test mode finished")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Microfone module: Vosk speech recognition")
    parser.add_argument(
        "--test",
        nargs="?",
        const="stt",
        choices=["stt", "audio", "scenario"],
        metavar="MODE",
        help="Test mode: stt (speech-to-text), audio (record and playback), scenario (wake word -> phrase -> command -> state write)",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="Print available audio devices and exit",
    )
    parser.add_argument(
        "--device-index",
        type=int,
        default=None,
        help="Input device index for microphone (default uses settings)",
    )
    parser.add_argument(
        "--wake-word",
        type=str,
        default=None,
        help="Override wake word (default from settings)",
    )
    parser.add_argument(
        "--command-seconds",
        type=float,
        default=None,
        help="Override command record duration in seconds (default from settings)",
    )
    parser.add_argument(
        "--model-path",
        type=str,
        default=None,
        help="Override Vosk model path (default from settings or VOSK_MODEL_PATH env)",
    )
    return parser.parse_args()


def build_config_from_args(args: argparse.Namespace) -> MicrophoneConfig:
    config = MicrophoneConfig()
    if args.device_index is not None:
        config.device_index = args.device_index
    if args.wake_word is not None and args.wake_word.strip():
        config.wake_word = args.wake_word.strip()
    if args.command_seconds is not None and args.command_seconds > 0:
        config.command_record_s = args.command_seconds
    if args.model_path is not None and args.model_path.strip():
        config.vosk_model_path = args.model_path.strip()
    return config


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    args = parse_args()

    if args.list_devices:
        if sd is None:
            raise RuntimeError(f"sounddevice not installed. {_venv_hint()}")
        print(sd.query_devices())
        return

    config = build_config_from_args(args)
    if args.test:
        if args.test == "audio":
            run_test_audio_mode(config)
        elif args.test == "scenario":
            run_test_scenario_mode(config)
        else:
            run_test_mode(config)
        return

    stop_event = threading.Event()
    try:
        run_microphone_loop(config, stop_event=stop_event)
    except KeyboardInterrupt:
        LOGGER.info("Microphone stopped by user")
    finally:
        stop_event.set()


if __name__ == "__main__":
    main()
