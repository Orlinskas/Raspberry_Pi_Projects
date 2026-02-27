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
    "STEP_FORWARD",
    "STEP_BACKWARD",
    "TURN_LEFT_15",
    "TURN_LEFT_45",
    "TURN_RIGHT_15",
    "TURN_RIGHT_45",
    "STOP",
    "LIGHT_ON",
    "LIGHT_OFF",
]

# Параметры движений (все заранее заданы; command.json не содержит params)
ACTION_DURATION_MS = {
    "STEP_FORWARD": 1000,
    "STEP_BACKWARD": 500,
    "TURN_LEFT_15": 200,
    "TURN_LEFT_45": 400,
    "TURN_RIGHT_15": 200,
    "TURN_RIGHT_45": 400,
}
ACTION_SPEED = {
    "STEP_FORWARD": 20,
    "STEP_BACKWARD": 40,
    "TURN_LEFT_15": 50,
    "TURN_LEFT_45": 50,
    "TURN_RIGHT_15": 50,
    "TURN_RIGHT_45": 50,
}


def get_effective_duration_ms(action: str) -> int:
    """Возвращает duration_ms для действия (0 для STOP, LIGHT_*)."""
    return ACTION_DURATION_MS.get(action, 0)


TURN_DURATION_MS = {k: v for k, v in ACTION_DURATION_MS.items() if k.startswith("TURN_")}
TURN_SPEED = {k: v for k, v in ACTION_SPEED.items() if k.startswith("TURN_")}
TURN_ACTIONS = frozenset(TURN_DURATION_MS.keys())

DEFAULT_GRID_5X5 = [
    ".....",
    ".....",
    "..R..",
    ".....",
    ".....",
]

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
            "grid": DEFAULT_GRID_5X5,
            "description": None,
            "target_x": None,
        },
    }

def zero_command_payload() -> Dict[str, Any]:
    """Возвращает нулевую команду STOP."""
    return {
        "command_id": "cmd_000000",
        "timestamp": 0.0,
        "based_on_state_id": "st_000000",
        "action": "STOP",
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
    """Состояние камеры/детектора. grid — 5x5 массив строк: '.' free, 'R' robot, 'O' obstacle, 'T' target."""

    grid: Optional[list] = field(default_factory=lambda: list(DEFAULT_GRID_5X5))
    description: Optional[str] = None
    target_x: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "grid": self.grid,
            "description": self.description,
            "target_x": self.target_x,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "CameraState":
        grid = payload.get("grid")
        if not isinstance(grid, list) or len(grid) != 5 or not all(isinstance(r, str) and len(r) == 5 for r in grid):
            grid = DEFAULT_GRID_5X5
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
            grid=grid,
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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state_id": self.state_id,
            "timestamp": self.timestamp,
            "sensor": self.sensor.to_dict(),
            "camera": self.camera.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "RobotState":
        sensor_payload = payload.get("sensor", payload.get("proximity", {}))
        cam = payload.get("camera", {})
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
        )


@dataclass
class RobotCommand:
    """Команда управления роботом (выход brain, вход controller).
    Параметры движения (speed, duration_ms) заданы в shared.ACTION_SPEED и ACTION_DURATION_MS.
    """

    command_id: str = ""
    timestamp: float = field(default_factory=now_ts)
    based_on_state_id: str = ""
    action: str = "STOP"
    reason: str = "default_stop"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command_id": self.command_id,
            "timestamp": self.timestamp,
            "based_on_state_id": self.based_on_state_id,
            "action": self.action,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "RobotCommand":
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
            reason=str(payload.get("reason", "unspecified")),
        )
