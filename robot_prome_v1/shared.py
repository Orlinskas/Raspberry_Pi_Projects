#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Общие модели и утилиты протокола для взаимодействия модулей робота.

Этот файл содержит:
- контракты `protocol/state.json` и `protocol/command.json`;
- безопасный atomic-write JSON (без битых файлов при падении);
- проверку "устаревших" данных по timestamp.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Union

ACTIONS = [
    "FORWARD",
    "BACKWARD",
    "TURN_LEFT_15",
    "TURN_LEFT_45",
    "TURN_RIGHT_15",
    "TURN_RIGHT_45",
    "STOP",
    "LIGHT_ON",
    "LIGHT_OFF",
]

# Параметры поворотов (подбираются отдельно; command.params для поворотов игнорируются)
TURN_DURATION_MS = {
    "TURN_LEFT_15": 200,
    "TURN_LEFT_45": 500,
    "TURN_RIGHT_15": 200,
    "TURN_RIGHT_45": 500,
}
TURN_SPEED = {
    "TURN_LEFT_15": 40,
    "TURN_LEFT_45": 40,
    "TURN_RIGHT_15": 40,
    "TURN_RIGHT_45": 40,
}


def get_effective_duration_ms(action: str, params_duration_ms: int) -> int:
    """Возвращает duration_ms для действия: для поворотов — из TURN_DURATION_MS, иначе из params."""
    if action in TURN_DURATION_MS:
        return TURN_DURATION_MS[action]
    return params_duration_ms


TURN_ACTIONS = frozenset(TURN_DURATION_MS.keys())

PathLike = Union[str, Path]
GPIO_LOCK = threading.RLock()


def zero_state_payload() -> Dict[str, Any]:
    """Возвращает нулевое (стартовое) состояние робота."""
    return {
        "state_id": "st_000000",
        "timestamp": 0.0,
        "sensor": {
            "obstacle_cm": None,
        },
        "camera": {
            "scene_map": {
                "grid_size": 7,
                "robot_cell": {"row": 3, "col": 3},
                "grid": [
                    ".......",
                    ".......",
                    ".......",
                    "...R...",
                    ".......",
                    ".......",
                    ".......",
                ],
                "legend": {
                    ".": "free",
                    "R": "robot",
                    "O": "obstacle",
                    "T": "target",
                },
            },
            "description": None,
            "target_x": None,
        },
        "last_command": {
            "last_action": "STOP",
            "reason": "initial_state",
            "remaining_ms": 0,
        },
    }

def zero_command_payload() -> Dict[str, Any]:
    """Возвращает нулевую команду STOP."""
    return {
        "command_id": "cmd_000000",
        "timestamp": 0.0,
        "based_on_state_id": "st_000000",
        "action": "STOP",
        "params": {
            "speed": 0,
            "duration_ms": 0,
        },
        "reason": "initial_state",
    }


def now_ts() -> float:
    """Текущее время в формате Unix timestamp."""
    return time.time()


def is_stale(timestamp: float, max_age_ms: int, now: Optional[float] = None) -> bool:
    """Проверяет, не устарели ли данные относительно текущего времени."""
    ts_now = now if now is not None else now_ts()
    return (ts_now - timestamp) * 1000.0 > float(max_age_ms)


def atomic_write_json(path: PathLike, payload: Dict[str, Any]) -> None:
    """Атомарно записывает JSON через временный файл + replace()."""
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
    """Безопасно читает JSON-объект; при ошибке возвращает None."""
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
    """Состояние датчика приближения."""

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
    """Состояние камеры/детектора."""

    scene_map: Optional[Dict[str, Any]] = None
    description: Optional[str] = None
    target_x: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scene_map": self.scene_map,
            "description": self.description,
            "target_x": self.target_x,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "CameraState":
        scene_map = payload.get("scene_map")
        if not isinstance(scene_map, dict):
            scene_map = None
        target_x = payload.get("target_x")
        try:
            target_x = float(target_x) if target_x is not None else None
        except (TypeError, ValueError):
            target_x = None
        if target_x is not None:
            target_x = max(-1.0, min(1.0, target_x))
        description = payload.get("description")
        if description is not None:
            description = str(description).strip() or None
        return cls(
            scene_map=scene_map,
            description=description,
            target_x=target_x,
        )


@dataclass
class RobotState:
    """Полный снимок состояния робота (выход vision)."""

    state_id: str = ""
    timestamp: float = field(default_factory=now_ts)
    sensor: ProximityState = field(default_factory=ProximityState)
    camera: CameraState = field(default_factory=CameraState)
    last_command: "FeelingsState" = field(default_factory=lambda: FeelingsState())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state_id": self.state_id,
            "timestamp": self.timestamp,
            "sensor": self.sensor.to_dict(),
            "camera": self.camera.to_dict(),
            "last_command": self.last_command.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "RobotState":
        sensor_payload = payload.get("sensor", payload.get("proximity", {}))
        cam = payload.get("camera", {})
        last_command_payload = payload.get("last_command", payload.get("feelings", {}))
        timestamp = payload.get("timestamp", now_ts())
        try:
            timestamp = float(timestamp)
        except (TypeError, ValueError):
            timestamp = now_ts()
        return cls(
            state_id=str(payload.get("state_id", "")),
            timestamp=timestamp,
            sensor=ProximityState.from_dict(sensor_payload if isinstance(sensor_payload, dict) else {}),
            camera=CameraState.from_dict(cam if isinstance(cam, dict) else {}),
            last_command=FeelingsState.from_dict(last_command_payload if isinstance(last_command_payload, dict) else {}),
        )


@dataclass
class CommandParams:
    """Параметры движения для controller."""

    speed: int = 20
    duration_ms: int = 200

    def to_dict(self) -> Dict[str, Any]:
        return {"speed": int(self.speed), "duration_ms": int(self.duration_ms)}

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "CommandParams":
        speed = payload.get("speed", 20)
        duration_ms = payload.get("duration_ms", 200)
        try:
            speed = int(speed)
        except (TypeError, ValueError):
            speed = 20
        try:
            duration_ms = int(duration_ms)
        except (TypeError, ValueError):
            duration_ms = 200
        return cls(speed=max(0, min(100, speed)), duration_ms=max(0, duration_ms))


@dataclass
class FeelingsState:
    """Текущее исполняемое действие, привязанное к последней команде."""

    last_action: str = "STOP"
    reason: str = "initial_state"
    remaining_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "last_action": self.last_action,
            "reason": self.reason,
            "remaining_ms": int(self.remaining_ms),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "FeelingsState":
        remaining_ms = payload.get("remaining_ms", 0)
        try:
            remaining_ms = int(remaining_ms)
        except (TypeError, ValueError):
            remaining_ms = 0
        last_action = str(payload.get("last_action", payload.get("action", "STOP"))).upper()
        if last_action not in ACTIONS:
            last_action = "STOP"
        return cls(
            last_action=last_action,
            reason=str(payload.get("reason", "unspecified")),
            remaining_ms=max(0, remaining_ms),
        )


@dataclass
class RobotCommand:
    """Команда управления роботом (выход brain, вход controller)."""

    command_id: str = ""
    timestamp: float = field(default_factory=now_ts)
    based_on_state_id: str = ""
    action: str = "STOP"
    params: CommandParams = field(default_factory=CommandParams)
    reason: str = "default_stop"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command_id": self.command_id,
            "timestamp": self.timestamp,
            "based_on_state_id": self.based_on_state_id,
            "action": self.action,
            "params": self.params.to_dict(),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "RobotCommand":
        params = payload.get("params", {})
        timestamp = payload.get("timestamp", now_ts())
        try:
            timestamp = float(timestamp)
        except (TypeError, ValueError):
            timestamp = now_ts()
        action = str(payload.get("action", "STOP")).upper()
        if action not in ACTIONS:
            action = "STOP"
        return cls(
            command_id=str(payload.get("command_id", "")),
            timestamp=timestamp,
            based_on_state_id=str(payload.get("based_on_state_id", "")),
            action=action,
            params=CommandParams.from_dict(params if isinstance(params, dict) else {}),
            reason=str(payload.get("reason", "unspecified")),
        )
