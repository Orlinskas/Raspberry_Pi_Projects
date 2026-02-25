#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Модуль brain: читает `state.json`, принимает решение и пишет `command.json`."""

from __future__ import annotations

import argparse
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from shared import (
    CommandParams,
    CommandSafety,
    RobotCommand,
    RobotState,
    atomic_write_json,
    is_stale,
    now_ts,
    read_json,
)

LOGGER = logging.getLogger("brain")


@dataclass
class BrainConfig:
    """Настройки логики принятия решений."""

    state_path: Path = Path(__file__).with_name("state.json")
    command_path: Path = Path(__file__).with_name("command.json")
    interval_s: float = 0.2
    state_timeout_ms: int = 700
    obstacle_distance_cm: float = 25.0
    camera_confidence_threshold: float = 0.7
    target_deadband: float = 0.25
    min_action_hold_ms: int = 350


class BrainEngine:
    """Ядро принятия решений `observe -> interpret -> decide`."""

    def __init__(self, config: BrainConfig) -> None:
        self.config = config
        self._last_action = "STOP"
        self._last_action_ts = 0.0
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
            safety=CommandSafety(cancel_if_state_older_ms=self.config.state_timeout_ms),
        )

    def _apply_hysteresis(self, candidate: RobotCommand) -> RobotCommand:
        """Защита от "дребезга" команд: не переключаться слишком быстро."""
        ts_now = now_ts()
        if candidate.action != self._last_action:
            if self._last_action_ts > 0 and not is_stale(self._last_action_ts, self.config.min_action_hold_ms, ts_now):
                return self._new_command(
                    action=self._last_action,
                    state_id=candidate.based_on_state_id,
                    reason=f"hysteresis_hold_{candidate.reason}",
                    speed=candidate.params.speed,
                    duration_ms=candidate.params.duration_ms,
                )
            self._last_action = candidate.action
            self._last_action_ts = ts_now
        return candidate

    def decide(self, state: Optional[RobotState]) -> RobotCommand:
        """Основная стратегия принятия решения для текущего state."""
        if state is None:
            return self._new_command("STOP", "unknown", "state_missing", speed=0, duration_ms=0)

        ts_now = now_ts()
        if is_stale(state.timestamp, self.config.state_timeout_ms, ts_now):
            return self._new_command("STOP", state.state_id, "state_stale", speed=0, duration_ms=0)

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
            candidate = self._new_command("TURN_LEFT", state.state_id, "fused_obstacle_detected", speed=55, duration_ms=350)
        elif proximity_danger:
            candidate = self._new_command("TURN_LEFT", state.state_id, "proximity_obstacle_detected", speed=50, duration_ms=300)
        elif camera_danger:
            candidate = self._new_command("TURN_LEFT", state.state_id, "camera_obstacle_detected", speed=45, duration_ms=280)
        elif state.camera.valid and state.camera.target_x is not None:
            if state.camera.target_x > self.config.target_deadband:
                candidate = self._new_command("TURN_RIGHT", state.state_id, "target_alignment_right", speed=35, duration_ms=180)
            elif state.camera.target_x < -self.config.target_deadband:
                candidate = self._new_command("TURN_LEFT", state.state_id, "target_alignment_left", speed=35, duration_ms=180)
            else:
                candidate = self._new_command("FORWARD", state.state_id, "target_centered_path_clear", speed=45, duration_ms=220)
        elif not state.proximity.valid and not state.camera.valid:
            candidate = self._new_command("STOP", state.state_id, "all_sensors_invalid", speed=0, duration_ms=0)
        else:
            candidate = self._new_command("FORWARD", state.state_id, "path_clear_default", speed=40, duration_ms=220)

        return self._apply_hysteresis(candidate)


def run_brain_loop(config: BrainConfig, stop_event: Optional[threading.Event] = None) -> None:
    """Цикл brain: прочитал state -> принял решение -> записал command."""
    stop_event = stop_event or threading.Event()
    engine = BrainEngine(config)
    LOGGER.info("Brain запущен. state=%s command=%s", config.state_path, config.command_path)

    while not stop_event.is_set():
        raw_state = read_json(config.state_path)
        state = RobotState.from_dict(raw_state) if isinstance(raw_state, dict) else None
        command = engine.decide(state)
        atomic_write_json(config.command_path, command.to_dict())
        LOGGER.debug("Опубликован command_id=%s action=%s reason=%s", command.command_id, command.action, command.reason)
        stop_event.wait(config.interval_s)

    LOGGER.info("Brain остановлен")


def parse_args() -> BrainConfig:
    parser = argparse.ArgumentParser(description="Brain module")
    parser.add_argument("--state-path", default=str(Path(__file__).with_name("state.json")))
    parser.add_argument("--command-path", default=str(Path(__file__).with_name("command.json")))
    parser.add_argument("--interval", type=float, default=0.2)
    parser.add_argument("--state-timeout-ms", type=int, default=700)
    args = parser.parse_args()
    return BrainConfig(
        state_path=Path(args.state_path),
        command_path=Path(args.command_path),
        interval_s=max(0.05, float(args.interval)),
        state_timeout_ms=max(200, int(args.state_timeout_ms)),
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
