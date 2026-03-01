#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Union

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


def get_effective_duration_ms(action: str) -> int:
    return ACTION_DURATION_MS.get(action, 0)


TURN_DURATION_MS = {k: v for k, v in ACTION_DURATION_MS.items() if k.startswith("TURN_")}
TURN_SPEED = {k: v for k, v in ACTION_SPEED.items() if k.startswith("TURN_")}
TURN_ACTIONS = frozenset(TURN_DURATION_MS.keys())

PathLike = Union[str, Path]
GPIO_LOCK = threading.RLock()


def zero_state_payload() -> Dict[str, Any]:
    return {
        "state_id": "st_000000",
        "sensor": {
            "obstacle_cm": None,
        },
        "camera": {
            "image_path": None,
        },
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
