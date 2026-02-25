#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Модуль brain: читает `state.json`, принимает решение и пишет `command.json`."""

from __future__ import annotations

import argparse
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from shared import (
    CommandParams,
    RobotCommand,
    RobotState,
    atomic_write_json,
    now_ts,
    read_json,
)

LOGGER = logging.getLogger("brain")
POLL_WAIT_S = 0.05


def _json_line(payload) -> str:
    """Возвращает JSON с отступами для удобного чтения в консоли."""
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


@dataclass
class BrainConfig:
    """Настройки путей и порогов логики brain."""

    state_path: Path = Path(__file__).with_name("state.json")
    command_path: Path = Path(__file__).with_name("command.json")
    obstacle_distance_cm: float = 30.0
    camera_confidence_threshold: float = 0.7
    target_deadband: float = 0.25


class BrainEngine:
    """Ядро принятия решений `state -> command`."""

    def __init__(self, config: BrainConfig) -> None:
        self.config = config
        self._counter = 0

    def _new_command(
        self,
        action: str,
        state_id: str,
        reason: str,
        speed: int = 40,
        duration_ms: int = 200,
    ) -> RobotCommand:
        """Формирует типовую команду с инкрементным id."""
        self._counter += 1
        return RobotCommand(
            command_id=f"cmd_{self._counter:06d}",
            timestamp=now_ts(),
            based_on_state_id=state_id,
            action=action,
            params=CommandParams(speed=speed, duration_ms=duration_ms),
            reason=reason,
        )

    def decide(self, state: Optional[RobotState]) -> RobotCommand:
        """Основная стратегия принятия решения для нового state."""
        if state is None:
            return self._new_command("STOP", "unknown", "state_missing", speed=0, duration_ms=0)

        proximity_danger = (
            state.proximity.valid
            and state.proximity.distance_cm is not None
            and state.proximity.distance_cm < self.config.obstacle_distance_cm
        )
        camera_danger = (
            state.camera.valid
            and state.camera.obstacle
            and state.camera.confidence >= self.config.camera_confidence_threshold
        )

        if proximity_danger and camera_danger:
            candidate = self._new_command("TURN_LEFT", state.state_id, "fused_obstacle_detected", speed=25, duration_ms=350)
        elif proximity_danger:
            candidate = self._new_command("TURN_LEFT", state.state_id, "proximity_obstacle_detected", speed=30, duration_ms=300)
        elif camera_danger:
            candidate = self._new_command("TURN_LEFT", state.state_id, "camera_obstacle_detected", speed=15, duration_ms=280)
        elif state.camera.valid and state.camera.target_x is not None:
            if state.camera.target_x > self.config.target_deadband:
                candidate = self._new_command("TURN_RIGHT", state.state_id, "target_alignment_right", speed=15, duration_ms=180)
            elif state.camera.target_x < -self.config.target_deadband:
                candidate = self._new_command("TURN_LEFT", state.state_id, "target_alignment_left", speed=25, duration_ms=180)
            else:
                candidate = self._new_command("FORWARD", state.state_id, "target_centered_path_clear", speed=35, duration_ms=220)
        elif not state.proximity.valid and not state.camera.valid:
            candidate = self._new_command("STOP", state.state_id, "all_sensors_invalid", speed=0, duration_ms=0)
        else:
            candidate = self._new_command("FORWARD", state.state_id, "path_clear_default", speed=30, duration_ms=220)

        return candidate


def run_brain_loop(config: BrainConfig, stop_event: Optional[threading.Event] = None) -> None:
    """Цикл brain: обрабатывает только новый state_id."""
    stop_event = stop_event or threading.Event()
    engine = BrainEngine(config)
    LOGGER.info("Brain запущен. state=%s command=%s", config.state_path, config.command_path)
    last_state_id = ""

    while not stop_event.is_set():
        raw_state = read_json(config.state_path)
        if not isinstance(raw_state, dict):
            stop_event.wait(POLL_WAIT_S)
            continue

        state = RobotState.from_dict(raw_state)
        if not state.state_id:
            stop_event.wait(POLL_WAIT_S)
            continue

        if state.state_id == last_state_id:
            stop_event.wait(POLL_WAIT_S)
            continue

        LOGGER.info("STATE used:\n%s", _json_line(raw_state))
        command = engine.decide(state)
        command_payload = command.to_dict()
        atomic_write_json(config.command_path, command_payload)
        LOGGER.info("COMMAND generated:\n%s", _json_line(command_payload))
        last_state_id = state.state_id

    LOGGER.info("Brain остановлен")


def parse_args() -> BrainConfig:
    parser = argparse.ArgumentParser(description="Brain module")
    parser.add_argument("--state-path", default=str(Path(__file__).with_name("state.json")))
    parser.add_argument("--command-path", default=str(Path(__file__).with_name("command.json")))
    args = parser.parse_args()
    return BrainConfig(
        state_path=Path(args.state_path),
        command_path=Path(args.command_path),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    config = parse_args()
    try:
        run_brain_loop(config)
    except KeyboardInterrupt:
        LOGGER.info("Brain остановлен пользователем")


if __name__ == "__main__":
    main()
