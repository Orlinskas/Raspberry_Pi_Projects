#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Общие модели и утилиты протокола для взаимодействия модулей робота.

Этот файл содержит:
- контракты `state.json` и `command.json`;
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

SCHEMA_VERSION = "1.0"
ACTIONS = {"FORWARD", "BACKWARD", "TURN_LEFT", "TURN_RIGHT", "STOP"}

PathLike = Union[str, Path]
GPIO_LOCK = threading.RLock()


def zero_state_payload() -> Dict[str, Any]:
    """Возвращает нулевое (стартовое) состояние робота."""
    return {
        "schema_version": SCHEMA_VERSION,
        "state_id": "st_000000",
        "timestamp": 0.0,
        "proximity": {
            "distance_cm": None,
            "valid": False,
        },
        "camera": {
            "obstacle": False,
            "target_x": None,
            "confidence": 0.0,
            "valid": False,
        },
        "feelings": {
            "command_id": "cmd_000000",
            "action": "STOP",
            "speed": 0,
            "duration_ms": 0,
            "reason": "initial_state",
            "updated_at": 0.0,
        },
    }


def zero_command_payload() -> Dict[str, Any]:
    """Возвращает нулевую команду STOP."""
    return {
        "schema_version": SCHEMA_VERSION,
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

    distance_cm: Optional[float] = None
    valid: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"distance_cm": self.distance_cm, "valid": self.valid}

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "ProximityState":
        return cls(distance_cm=payload.get("distance_cm"), valid=bool(payload.get("valid", False)))


@dataclass
class CameraState:
    """Состояние камеры/детектора."""

    obstacle: bool = False
    target_x: Optional[float] = None
    confidence: float = 0.0
    valid: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "obstacle": self.obstacle,
            "target_x": self.target_x,
            "confidence": self.confidence,
            "valid": self.valid,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "CameraState":
        confidence = payload.get("confidence", 0.0)
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = 0.0
        return cls(
            obstacle=bool(payload.get("obstacle", False)),
            target_x=payload.get("target_x"),
            confidence=max(0.0, min(1.0, confidence)),
            valid=bool(payload.get("valid", False)),
        )


@dataclass
class RobotState:
    """Полный снимок состояния робота (выход vision)."""

    schema_version: str = SCHEMA_VERSION
    state_id: str = ""
    timestamp: float = field(default_factory=now_ts)
    proximity: ProximityState = field(default_factory=ProximityState)
    camera: CameraState = field(default_factory=CameraState)
    feelings: "FeelingsState" = field(default_factory=lambda: FeelingsState())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "state_id": self.state_id,
            "timestamp": self.timestamp,
            "proximity": self.proximity.to_dict(),
            "camera": self.camera.to_dict(),
            "feelings": self.feelings.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "RobotState":
        prox = payload.get("proximity", {})
        cam = payload.get("camera", {})
        feelings = payload.get("feelings", {})
        timestamp = payload.get("timestamp", now_ts())
        try:
            timestamp = float(timestamp)
        except (TypeError, ValueError):
            timestamp = now_ts()
        return cls(
            schema_version=str(payload.get("schema_version", SCHEMA_VERSION)),
            state_id=str(payload.get("state_id", "")),
            timestamp=timestamp,
            proximity=ProximityState.from_dict(prox if isinstance(prox, dict) else {}),
            camera=CameraState.from_dict(cam if isinstance(cam, dict) else {}),
            feelings=FeelingsState.from_dict(feelings if isinstance(feelings, dict) else {}),
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

    command_id: str = "cmd_000000"
    action: str = "STOP"
    speed: int = 0
    duration_ms: int = 0
    reason: str = "initial_state"
    updated_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command_id": self.command_id,
            "action": self.action,
            "speed": int(self.speed),
            "duration_ms": int(self.duration_ms),
            "reason": self.reason,
            "updated_at": float(self.updated_at),
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "FeelingsState":
        speed = payload.get("speed", 0)
        duration_ms = payload.get("duration_ms", 0)
        updated_at = payload.get("updated_at", 0.0)
        try:
            speed = int(speed)
        except (TypeError, ValueError):
            speed = 0
        try:
            duration_ms = int(duration_ms)
        except (TypeError, ValueError):
            duration_ms = 0
        try:
            updated_at = float(updated_at)
        except (TypeError, ValueError):
            updated_at = 0.0
        action = str(payload.get("action", "STOP")).upper()
        if action not in ACTIONS:
            action = "STOP"
        return cls(
            command_id=str(payload.get("command_id", "cmd_000000")),
            action=action,
            speed=max(0, min(100, speed)),
            duration_ms=max(0, duration_ms),
            reason=str(payload.get("reason", "unspecified")),
            updated_at=max(0.0, updated_at),
        )


@dataclass
class RobotCommand:
    """Команда управления роботом (выход brain, вход controller)."""

    schema_version: str = SCHEMA_VERSION
    command_id: str = ""
    timestamp: float = field(default_factory=now_ts)
    based_on_state_id: str = ""
    action: str = "STOP"
    params: CommandParams = field(default_factory=CommandParams)
    reason: str = "default_stop"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
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
            schema_version=str(payload.get("schema_version", SCHEMA_VERSION)),
            command_id=str(payload.get("command_id", "")),
            timestamp=timestamp,
            based_on_state_id=str(payload.get("based_on_state_id", "")),
            action=action,
            params=CommandParams.from_dict(params if isinstance(params, dict) else {}),
            reason=str(payload.get("reason", "unspecified")),
        )
