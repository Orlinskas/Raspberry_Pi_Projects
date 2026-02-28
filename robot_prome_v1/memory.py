#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Модуль memory: следит за command.json и записывает историю в memory.json."""

from __future__ import annotations

import argparse
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from shared import atomic_write_json, read_json, zero_memory_payload

LOGGER = logging.getLogger("memory")
POLL_WAIT_S = 0.1
MEMORY_MAX_ENTRIES = 10


@dataclass
class MemoryConfig:
    """Настройки модуля памяти."""

    state_path: Path = Path(__file__).with_name("protocol") / "state.json"
    command_path: Path = Path(__file__).with_name("protocol") / "command.json"
    memory_path: Path = Path(__file__).with_name("protocol") / "memory.json"
    max_entries: int = MEMORY_MAX_ENTRIES


def _ensure_memory_file(memory_path: Path) -> None:
    """Создаёт memory.json с пустым action_history, если файла нет."""
    if memory_path.exists():
        return
    atomic_write_json(memory_path, zero_memory_payload())
    LOGGER.info("Создан пустой memory.json")


def _read_memory(memory_path: Path) -> Dict[str, Any]:
    """Читает memory.json, возвращает валидный payload."""
    raw = read_json(memory_path)
    if not isinstance(raw, dict):
        return zero_memory_payload()
    history = raw.get("action_history")
    if not isinstance(history, list):
        return zero_memory_payload()
    return {"action_history": history}


def _append_entry(
    memory_path: Path,
    entry: Dict[str, Any],
    max_entries: int,
) -> None:
    """Добавляет запись в memory, обрезает до max_entries."""
    data = _read_memory(memory_path)
    history: List[Dict[str, Any]] = list(data["action_history"])
    history.append(entry)
    if len(history) > max_entries:
        history = history[-max_entries:]
    data["action_history"] = history
    atomic_write_json(memory_path, data)
    LOGGER.debug("Добавлена запись в memory: %s", entry.get("command_id"))


def run_memory_loop(config: MemoryConfig, stop_event: Optional[threading.Event] = None) -> None:
    """Цикл memory: следит за command_id, при новой команде дописывает в memory.json."""
    stop_event = stop_event or threading.Event()
    _ensure_memory_file(config.memory_path)

    last_command_id = ""
    LOGGER.info("Memory запущен. command=%s memory=%s", config.command_path, config.memory_path)

    while not stop_event.is_set():
        raw_command = read_json(config.command_path)
        if not isinstance(raw_command, dict):
            stop_event.wait(POLL_WAIT_S)
            continue

        command_id = str(raw_command.get("command_id", ""))
        if not command_id or command_id == last_command_id:
            stop_event.wait(POLL_WAIT_S)
            continue

        # Новая команда — читаем state и формируем запись
        raw_state = read_json(config.state_path)
        obstacle_cm = None
        if isinstance(raw_state, dict):
            sensor = raw_state.get("sensor", raw_state.get("proximity", {}))
            if isinstance(sensor, dict):
                try:
                    obstacle_cm = float(sensor.get("obstacle_cm")) if sensor.get("obstacle_cm") is not None else None
                except (TypeError, ValueError):
                    pass

        state_id = str(raw_command.get("based_on_state_id", ""))
        action = str(raw_command.get("action", "STOP"))
        reason = str(raw_command.get("reason", ""))

        entry = {
            "state_id": state_id,
            "command_id": command_id,
            "action": action,
            "reason": reason,
            "obstacle_cm": obstacle_cm,
        }
        _append_entry(config.memory_path, entry, config.max_entries)
        last_command_id = command_id

    LOGGER.info("Memory остановлен")


def get_recent_actions(memory_path: Path, limit: int = 8) -> List[Dict[str, Any]]:
    """Возвращает последние limit записей из memory (для brain)."""
    data = _read_memory(memory_path)
    history = data.get("action_history", [])
    if not isinstance(history, list):
        return []
    valid = [e for e in history if isinstance(e, dict)]
    return valid[-limit:]


def parse_args() -> MemoryConfig:
    parser = argparse.ArgumentParser(description="Memory module")
    parser.add_argument("--state-path", default=str(MemoryConfig.state_path), help="Path to protocol/state.json")
    parser.add_argument("--command-path", default=str(MemoryConfig.command_path), help="Path to protocol/command.json")
    parser.add_argument("--memory-path", default=str(MemoryConfig.memory_path), help="Path to protocol/memory.json")
    parser.add_argument("--max-entries", type=int, default=MEMORY_MAX_ENTRIES, help="Max action_history entries")
    args = parser.parse_args()
    return MemoryConfig(
        state_path=Path(args.state_path),
        command_path=Path(args.command_path),
        memory_path=Path(args.memory_path),
        max_entries=max(1, int(args.max_entries)),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    config = parse_args()
    try:
        run_memory_loop(config)
    except KeyboardInterrupt:
        LOGGER.info("Memory остановлен пользователем")


if __name__ == "__main__":
    main()
