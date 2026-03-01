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
}

ACTION_SPEED = {
    "STEP_FORWARD": 20,
    "STEP_BACKWARD": 40,
    "TURN_LEFT_15": 25,
    "TURN_LEFT_45": 25,
    "TURN_RIGHT_15": 25,
    "TURN_RIGHT_45": 25,
    "PLAY": 60,
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

# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def zero_state_payload() -> Dict[str, Any]:
    return {
        "state_id": "st_000000",
        "sensor": {"obstacle_cm": None},
        "camera": {"image_path": None},
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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state_id": self.state_id,
            "sensor": self.sensor.to_dict(),
            "camera": self.camera.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "RobotState":
        sensor_payload = payload.get("sensor", payload.get("proximity", {}))
        cam = payload.get("camera", {})
        return cls(
            state_id=str(payload.get("state_id", "")),
            sensor=ProximityState.from_dict(sensor_payload if isinstance(sensor_payload, dict) else {}),
            camera=CameraState.from_dict(cam if isinstance(cam, dict) else {}),
        )


@dataclass
class RobotCommand:
    command_id: str = ""
    based_on_state_id: str = ""
    action: str = "LIGHT_OFF"
    reason: str = "default"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command_id": self.command_id,
            "based_on_state_id": self.based_on_state_id,
            "action": self.action,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "RobotCommand":
        action = str(payload.get("action", "LIGHT_OFF")).upper()
        if action not in ACTIONS:
            action = "LIGHT_OFF"
        return cls(
            command_id=str(payload.get("command_id", "")),
            based_on_state_id=str(payload.get("based_on_state_id", "")),
            action=action,
            reason=str(payload.get("reason", "unspecified")),
        )


# ---------------------------------------------------------------------------
# Brain
# ---------------------------------------------------------------------------

BRAIN_POLL_WAIT_S = 0.1
ROBOT_TASK = "Find and play with a toy"

def get_brain_system_prompt() -> str:
    allowed_actions = ", ".join(ACTIONS)
    return f"""You are a decision engine for a small (40x40 cm) robot.

**Task:** {ROBOT_TASK}.

You receive:
1. An image from the robot's front camera (what the robot sees ahead)
2. sensor.obstacle_cm — distance to the nearest obstacle in front, in cm (null if unavailable). Safe distance: >= 50 cm. Below 50 cm — be cautious (turn away or back up).
3. recent_actions — list of your last actions (state_id, action, reason, obstacle_cm). Use this to avoid repetitive loops.

Output ONLY a JSON object with keys: action, reason. Allowed action values: {allowed_actions}
Do not add markdown, comments, or extra keys.

## Commands

**Movement (drives a short distance then stops):**
- STEP_FORWARD — move forward
- STEP_BACKWARD — move backward

**Rotation (turns in place):**
- TURN_LEFT_15, TURN_RIGHT_15 — small correction (~15°)
- TURN_LEFT_45, TURN_RIGHT_45 — larger turn (~45°)

**Other:**
- LIGHT_ON — turn light on
- LIGHT_OFF — turn light off
- PLAY — celebrate: use when toy is found and close, or to express joy

## Rules

1. **Safety and navigation:**
   - Combine the image and sensor.obstacle_cm to assess the situation. Obstacles in the image (wall, furniture, chair, legs, person) and low distance readings both suggest caution — turn away or back up.
   - Obstacle on left → consider TURN_RIGHT. Obstacle on right → consider TURN_LEFT. Obstacle center → TURN_LEFT or TURN_RIGHT.

2. **Target seeking (when obstacle_cm >= 50 or null):**
   - Goal: keep the toy in the center of the image. If the toy is offset — turn towards it first. Do not drive forward past a toy that is off-center; you will miss it.
   - Toy on left side → TURN_LEFT_15 or TURN_LEFT_45 (turn until it moves toward center)
   - Toy at center → STEP_FORWARD
   - Toy on right side → TURN_RIGHT_15 or TURN_RIGHT_45 (turn until it moves toward center)
   - No toy visible → slow search turn (TURN_LEFT_15 or TURN_RIGHT_15) to find it
   - Toy found and close (obstacle_cm < 30 or toy fills center) → PLAY to celebrate

3. **Light:** If the image looks dark (low lighting, poorly lit room, shadows) — use LIGHT_ON to illuminate the scene. You can also use LIGHT_ON to draw attention to nearby toys. Use LIGHT_OFF when there is enough light. Avoid getting stuck in light-only loops (alternating LIGHT_ON/LIGHT_OFF repeatedly without movement).

4. **Use recent_actions:** You receive recent_actions (last actions taken). Use this history to avoid loops.

5. **Thinking** Keep reasoning short."""


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
STREAM_FPS = float(os.getenv("STREAM_FPS", "30"))  # 15-20 reasonable for Pi
STREAM_JPEG_QUALITY = int(os.getenv("STREAM_JPEG_QUALITY", "100"))  # 70-95, lower = faster


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
PLAY_CYCLES = 6
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

