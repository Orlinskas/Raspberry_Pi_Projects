#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# Author:   Vlad Orlinskas
# Site:     https://prometeriy.com
# Project:  robot_prome_v1 — LLM-driven autonomous robot experiment
# License:  Free for any use
# ---------------------------------------------------------------------------

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Union

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
PROTOCOL_DIR = ROOT / "protocol"
STATE_PATH = PROTOCOL_DIR / "state.json"
COMMAND_PATH = PROTOCOL_DIR / "command.json"
MEMORY_PATH = PROTOCOL_DIR / "memory.json"
CAPTURE_DIR = ROOT / "captures"

# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

ACTIONS = [
    "STEP_FORWARD",
    "STEP_BACKWARD",
    "TURN_LEFT_15",
    "TURN_LEFT_45",
    "TURN_RIGHT_15",
    "TURN_RIGHT_45",
    "LIGHT_ON",
    "LIGHT_OFF",
    "ERROR",
    "PLAY",
    "KILL",
]

ACTION_DURATION_MS = {
    "STEP_FORWARD": 1000,
    "STEP_BACKWARD": 500,
    "TURN_LEFT_15": 200,
    "TURN_LEFT_45": 600,
    "TURN_RIGHT_15": 200,
    "TURN_RIGHT_45": 600,
    "ERROR": 1000,
    "PLAY": 3500,
    "KILL": 5000,
}

ACTION_SPEED = {
    "STEP_FORWARD": 20,
    "STEP_BACKWARD": 40,
    "TURN_LEFT_15": 25,
    "TURN_LEFT_45": 25,
    "TURN_RIGHT_15": 25,
    "TURN_RIGHT_45": 25,
    "PLAY": 60,
    "KILL": 50,
}

TURN_DURATION_MS = {k: v for k, v in ACTION_DURATION_MS.items() if k.startswith("TURN_")}
TURN_SPEED = {k: v for k, v in ACTION_SPEED.items() if k.startswith("TURN_")}
TURN_ACTIONS = frozenset(TURN_DURATION_MS.keys())


def get_effective_duration_ms(action: str) -> int:
    return ACTION_DURATION_MS.get(action, 0)


# ---------------------------------------------------------------------------
# GPIO / threading
# ---------------------------------------------------------------------------

PathLike = Union[str, Path]
GPIO_LOCK = threading.RLock()

# Controller GPIO pins
CONTROLLER_IN1, CONTROLLER_IN2, CONTROLLER_IN3, CONTROLLER_IN4 = 20, 21, 19, 26
CONTROLLER_ENA, CONTROLLER_ENB = 16, 13
CONTROLLER_LED_R, CONTROLLER_LED_G, CONTROLLER_LED_B = 22, 27, 24
CONTROLLER_SERVO_PIN = 11  # FrontServoPin = 23 | ServoUpDownPin = 9 | ServoLeftRightPin = 11

# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def zero_state_payload() -> Dict[str, Any]:
    return {
        "state_id": "st_000000",
        "sensor": {"obstacle_cm": None},
        "camera": {"image_path": None},
        "command": "",
    }


def zero_command_payload() -> Dict[str, Any]:
    return {
        "command_id": "cmd_000000",
        "based_on_state_id": "st_000000",
        "action": "LIGHT_OFF",
        "reason": "initial_state",
    }


def zero_memory_payload() -> Dict[str, Any]:
    return {"action_history": []}


def atomic_write_json(path: PathLike, payload: Dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=target.name + ".", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def read_json(path: PathLike) -> Optional[Dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        return None
    try:
        with target.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if isinstance(raw, dict):
            return raw
    except (OSError, ValueError, TypeError):
        return None
    return None


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ProximityState:
    obstacle_cm: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"obstacle_cm": self.obstacle_cm}

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ProximityState":
        obstacle_cm = payload.get("obstacle_cm", payload.get("distance_cm"))
        try:
            obstacle_cm = float(obstacle_cm) if obstacle_cm is not None else None
        except (TypeError, ValueError):
            obstacle_cm = None
        return cls(obstacle_cm=obstacle_cm if obstacle_cm is None else max(0.0, obstacle_cm))


@dataclass
class CameraState:
    image_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"image_path": self.image_path}

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "CameraState":
        image_path = payload.get("image_path")
        if image_path is not None:
            image_path = str(image_path).strip() or None
        return cls(image_path=image_path)


@dataclass
class RobotState:
    state_id: str = ""
    sensor: ProximityState = field(default_factory=ProximityState)
    camera: CameraState = field(default_factory=CameraState)
    command: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state_id": self.state_id,
            "sensor": self.sensor.to_dict(),
            "camera": self.camera.to_dict(),
            "command": self.command,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "RobotState":
        sensor_payload = payload.get("sensor", payload.get("proximity", {}))
        cam = payload.get("camera", {})
        return cls(
            state_id=str(payload.get("state_id", "")),
            sensor=ProximityState.from_dict(sensor_payload if isinstance(sensor_payload, dict) else {}),
            camera=CameraState.from_dict(cam if isinstance(cam, dict) else {}),
            command=str(payload.get("command", "")).strip(),
        )


@dataclass
class RobotCommand:
    command_id: str = ""
    based_on_state_id: str = ""
    action: str = "LIGHT_OFF"
    reason: str = "default"
    voice: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "command_id": self.command_id,
            "based_on_state_id": self.based_on_state_id,
            "action": self.action,
            "reason": self.reason,
        }
        if self.voice is not None and self.voice.strip():
            result["voice"] = self.voice
        return result

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "RobotCommand":
        action = str(payload.get("action", "LIGHT_OFF")).upper()
        if action not in ACTIONS:
            action = "LIGHT_OFF"
        voice_raw = payload.get("voice")
        voice = str(voice_raw).strip() if voice_raw is not None else ""
        voice = voice or None
        return cls(
            command_id=str(payload.get("command_id", "")),
            based_on_state_id=str(payload.get("based_on_state_id", "")),
            action=action,
            reason=str(payload.get("reason", "unspecified")),
            voice=voice,
        )


# ---------------------------------------------------------------------------
# Brain
# ---------------------------------------------------------------------------

BRAIN_POLL_WAIT_S = 0.1
ROBOT_TASK = "Explore the world, be creative"
TARGET = "People"

def get_brain_system_prompt() -> str:
    allowed_actions = ", ".join(ACTIONS)
    return f"""You are a decision engine for a small (40x40 cm) robot.

Task: {ROBOT_TASK}.

You receive:
1) image: front camera image (may be missing)
2) sensor.obstacle_cm: distance to nearest front obstacle in cm (can be null)
3) recent_actions: recent history (state_id, action, reason, obstacle_cm, voice)
4) command: text command from human (can be empty)

Return ONLY one valid JSON object with keys:
- action
- reason
- voice

Allowed action values: {allowed_actions}

Strict output rules:
- No markdown
- No comments
- No extra keys
- No text before or after JSON

JSON example:
{{
  "action": "LIGHT_OFF",
  "reason": "short reason",
  "voice": "Короткая фраза по-русски"
}}

Behavior rules:
1) Safety first:
- If obstacle_cm is not null and < 50: avoid collision (TURN_* or STEP_BACKWARD).
- If obstacle appears in image, act cautiously even if the sensor is noisy.

2) Command priority:
- If command is non-empty, prioritize it immediately.
- Interpret command intent and choose the closest allowed action.

3) Target seeking:
- If obstacle_cm >= 50 or obstacle_cm is null, seek {TARGET}.
- Keep {TARGET} centered before moving forward.
- If {TARGET} is left -> TURN_LEFT_15 or TURN_LEFT_45
- If {TARGET} is right -> TURN_RIGHT_15 or TURN_RIGHT_45
- If {TARGET} is centered -> STEP_FORWARD
- If {TARGET} is not visible -> slow search turn (TURN_LEFT_15 or TURN_RIGHT_15)

4) Voice generation:
- voice must be in Russian.
- Keep it short (max ~12 words).
- Avoid repeating exactly the same recent voice phrase unless necessary.

5) Loop avoidance:
- Use recent_actions to avoid repeating the same action pattern many times in a row.

6) Thinking: 
- Keep reasoning short.
"""

@dataclass
class BrainConfig:
    state_path: Path = STATE_PATH
    command_path: Path = COMMAND_PATH
    memory_path: Path = MEMORY_PATH
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    ollama_model: str = os.getenv("OLLAMA_BRAIN_MODEL", "qwen3.5:397b-cloud")
    ollama_timeout_s: float = float(os.getenv("OLLAMA_TIMEOUT_S", "100"))
    llm_temperature: float = 0.1
    llm_num_predict: int = 512
    llm_keep_alive: str = os.getenv("OLLAMA_KEEP_ALIVE", "60m")
    log_llm_verbose: bool = False


# ---------------------------------------------------------------------------
# Vision
# ---------------------------------------------------------------------------

VISION_POLL_WAIT_S = 0.1
VISION_EXTRA_DELAY_S = 1.0

# Ultrasonic sensor
ECHO_PIN = 0
TRIG_PIN = 1
ULTRASONIC_TIMEOUT_S = 0.03
ULTRASONIC_MIN_CM = 2.0
ULTRASONIC_MAX_CM = 500.0
ULTRASONIC_INTER_MEASURE_DELAY_S = 0.06
ULTRASONIC_SAMPLES_PER_READ = 5
ULTRASONIC_OUTLIER_RATIO = 0.4

# Camera
CAMERA_INDEX = 0
CAMERA_WIDTH = 640
CAMERA_HEIGHT = 480
CAMERA_FPS = 30.0
CAMERA_WARMUP_S = 1.0
CAPTURE_KEEP_LAST = 30

STREAM_DEFAULT_PORT = 8765
STREAM_FPS = float(os.getenv("STREAM_FPS", "30"))
STREAM_JPEG_QUALITY = int(os.getenv("STREAM_JPEG_QUALITY", "100"))


@dataclass
class VisionConfig:
    state_path: Path = STATE_PATH
    capture_dir: Path = CAPTURE_DIR
    capture_keep_last: int = CAPTURE_KEEP_LAST
    command_path: Path = COMMAND_PATH
    stream_port: int = STREAM_DEFAULT_PORT
    stream_enabled: bool = True


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

CONTROLLER_POLL_INTERVAL_S = 0.05
PLAY_PHASE_DURATION_S = 0.2
PLAY_SPEED = 50
PLAY_CYCLES = 4
ERROR_BLINK_ON_S = 0.15
ERROR_BLINK_OFF_S = 0.15


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

MEMORY_POLL_WAIT_S = 0.1
MEMORY_MAX_ENTRIES = 10


@dataclass
class MemoryConfig:
    state_path: Path = STATE_PATH
    command_path: Path = COMMAND_PATH
    memory_path: Path = MEMORY_PATH
    max_entries: int = MEMORY_MAX_ENTRIES


# ---------------------------------------------------------------------------
# Audio playback (voice, microphone)
# ---------------------------------------------------------------------------
AUDIO_PLAYBACK_AMPLITUDE = 200
VOICE_MUTE_EVENT = threading.Event()

# ---------------------------------------------------------------------------
# Microphone
# ---------------------------------------------------------------------------

MICROPHONE_POLL_WAIT_S = 0.1
MICROPHONE_SAMPLE_RATE = 44100
MICROPHONE_CHANNELS = 1
MICROPHONE_DTYPE = "int16"
MICROPHONE_WAKE_WORD = "робот"
MICROPHONE_WAKE_WINDOW_S = 2.0
MICROPHONE_COMMAND_RECORD_S = 5.0
MICROPHONE_MIN_COMMAND_CHARS = 4
MICROPHONE_DEVICE_INDEX = -1  # -1 means default input device
MICROPHONE_VOSK_MODEL_PATH = os.getenv("VOSK_MODEL_PATH", str(ROOT / "vosk-model-small-ru-0.22"))
MICROPHONE_LOG_PARTIAL_RESULTS = False
MICROPHONE_RETRY_DELAY_S = 5.0

MICROPHONE_TEST_AUDIO_PLAY_TIMEOUT_S = 10.0
MICROPHONE_TEST_START_PROMPT = "Записываю"
MICROPHONE_TEST_DONE_STT_PROMPT = "Запись завершена"
MICROPHONE_TEST_DONE_AUDIO_PROMPT = "Запись завершена. Проигрываю"
MICROPHONE_TRIGGER_ACK_PROMPT = "Слушаю и выполняю"


@dataclass
class MicrophoneConfig:
    state_path: Path = STATE_PATH
    sample_rate: int = MICROPHONE_SAMPLE_RATE
    channels: int = MICROPHONE_CHANNELS
    dtype: str = MICROPHONE_DTYPE
    wake_word: str = MICROPHONE_WAKE_WORD
    wake_window_s: float = MICROPHONE_WAKE_WINDOW_S
    command_record_s: float = MICROPHONE_COMMAND_RECORD_S
    poll_interval_s: float = MICROPHONE_POLL_WAIT_S
    min_command_chars: int = MICROPHONE_MIN_COMMAND_CHARS
    device_index: int = MICROPHONE_DEVICE_INDEX
    vosk_model_path: str = MICROPHONE_VOSK_MODEL_PATH
    log_partial_results: bool = MICROPHONE_LOG_PARTIAL_RESULTS
    retry_delay_s: float = MICROPHONE_RETRY_DELAY_S
    test_audio_play_timeout_s: float = MICROPHONE_TEST_AUDIO_PLAY_TIMEOUT_S
    test_start_prompt: str = MICROPHONE_TEST_START_PROMPT
    test_done_stt_prompt: str = MICROPHONE_TEST_DONE_STT_PROMPT
    test_done_audio_prompt: str = MICROPHONE_TEST_DONE_AUDIO_PROMPT
    trigger_ack_prompt: str = MICROPHONE_TRIGGER_ACK_PROMPT

