#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Модуль feelings: обновляет краткий статус выполнения в state.last_command."""

from __future__ import annotations

import argparse
import logging
import threading
from pathlib import Path
from typing import Optional

from shared import FeelingsState, RobotCommand, RobotState, atomic_write_json, get_effective_duration_ms, now_ts, read_json

LOGGER = logging.getLogger("feelings")
STATE_PATH = Path(__file__).with_name("protocol") / "state.json"
COMMAND_PATH = Path(__file__).with_name("protocol") / "command.json"
POLL_INTERVAL_S = 0.1


def _build_feelings(command: RobotCommand) -> FeelingsState:
    duration_ms = get_effective_duration_ms(command.action, max(0, int(command.params.duration_ms)))
    ends_at = float(command.timestamp) + (duration_ms / 1000.0)
    remaining_ms = max(0, int((ends_at - now_ts()) * 1000.0))
    return FeelingsState(last_action=command.action, reason=command.reason, remaining_ms=remaining_ms)


def run_feelings_loop(stop_event: Optional[threading.Event] = None) -> None:
    stop_event = stop_event or threading.Event()
    LOGGER.info("Feelings запущен. state=%s command=%s", STATE_PATH, COMMAND_PATH)
    last_payload = None

    while not stop_event.is_set():
        raw_command = read_json(COMMAND_PATH)
        raw_state = read_json(STATE_PATH)
        if not isinstance(raw_command, dict) or not isinstance(raw_state, dict):
            stop_event.wait(POLL_INTERVAL_S)
            continue

        command = RobotCommand.from_dict(raw_command)
        state = RobotState.from_dict(raw_state)
        state.last_command = _build_feelings(command)
        payload = state.to_dict()
        if payload != last_payload:
            atomic_write_json(STATE_PATH, payload)
            last_payload = payload
        stop_event.wait(POLL_INTERVAL_S)

    LOGGER.info("Feelings остановлен")


def parse_args() -> None:
    parser = argparse.ArgumentParser(description="Feelings module")
    _ = parser.parse_args()
    return None


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    parse_args()
    try:
        run_feelings_loop()
    except KeyboardInterrupt:
        LOGGER.info("Feelings остановлен пользователем")


if __name__ == "__main__":
    main()
