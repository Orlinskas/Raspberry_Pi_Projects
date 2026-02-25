#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Точка входа: orchestrator, который запускает все модули робота."""

from __future__ import annotations

import argparse
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict

from brain import BrainConfig, run_brain_loop
from controller import run_controller_loop
from shared import is_stale, read_json
from vision import VisionConfig, run_vision_loop

LOGGER = logging.getLogger("main")


def _read_timestamp(payload: Dict[str, Any]) -> float:
    """Извлекает timestamp из JSON-подобного объекта."""
    if not isinstance(payload, dict):
        return 0.0
    value = payload.get("timestamp", 0.0)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def monitor_health(
    state_path: Path,
    command_path: Path,
    stop_event: threading.Event,
    check_interval_s: float = 0.5,
    stale_limit_ms: int = 2000,
) -> None:
    """Пассивный монитор: проверяет свежесть state/command."""
    LOGGER.info("Health monitor запущен")
    while not stop_event.is_set():
        state = read_json(state_path)
        command = read_json(command_path)

        if not isinstance(state, dict):
            LOGGER.warning("state.json отсутствует или поврежден")
        elif is_stale(_read_timestamp(state), stale_limit_ms):
            LOGGER.warning("state.json устарел")

        if isinstance(command, dict) and is_stale(_read_timestamp(command), stale_limit_ms):
            LOGGER.warning("command.json устарел")

        stop_event.wait(check_interval_s)
    LOGGER.info("Health monitor остановлен")


def parse_args():
    parser = argparse.ArgumentParser(description="Robot main orchestrator")
    parser.add_argument("--state-path", default=str(Path(__file__).with_name("state.json")))
    parser.add_argument("--command-path", default=str(Path(__file__).with_name("command.json")))
    parser.add_argument("--vision-interval", type=float, default=0.12)
    parser.add_argument("--brain-interval", type=float, default=0.2)
    parser.add_argument("--controller-poll", type=float, default=0.05)
    parser.add_argument("--real", action="store_true", help="Запуск с реальными сенсорами в vision")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    args = parse_args()

    state_path = Path(args.state_path)
    command_path = Path(args.command_path)

    stop_event = threading.Event()
    vision_config = VisionConfig(state_path=state_path, interval_s=max(0.03, args.vision_interval), use_mock=not bool(args.real))
    brain_config = BrainConfig(state_path=state_path, command_path=command_path, interval_s=max(0.05, args.brain_interval))

    threads = [
        threading.Thread(target=run_vision_loop, args=(vision_config, stop_event), name="vision", daemon=True),
        threading.Thread(target=run_brain_loop, args=(brain_config, stop_event), name="brain", daemon=True),
        threading.Thread(
            target=run_controller_loop,
            args=(command_path, max(0.02, args.controller_poll), stop_event),
            name="controller",
            daemon=True,
        ),
        threading.Thread(
            target=monitor_health,
            args=(state_path, command_path, stop_event),
            name="health-monitor",
            daemon=True,
        ),
    ]

    for thread in threads:
        thread.start()
        LOGGER.info("Запущен поток: %s", thread.name)

    try:
        while True:
            dead = [thread.name for thread in threads if not thread.is_alive()]
            if dead:
                # Любой критический сбой приводит к аварийному завершению оркестратора.
                LOGGER.error("Критические модули остановились: %s. Аварийная остановка.", ", ".join(dead))
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        LOGGER.info("Остановка пользователем")
    finally:
        stop_event.set()
        for thread in threads:
            thread.join(timeout=3.0)
        LOGGER.info("Main orchestrator остановлен")


if __name__ == "__main__":
    main()
