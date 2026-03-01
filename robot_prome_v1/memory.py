#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# Author:   Vlad Orlinskas
# Site:     https://prometeriy.com
# Project:  robot_prome_v1 — LLM-driven autonomous robot experiment
# License:  Free for any use
# ---------------------------------------------------------------------------

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional
from settings import (
    MEMORY_POLL_WAIT_S,
    MemoryConfig,
    atomic_write_json,
    read_json,
    zero_memory_payload,
)

LOGGER = logging.getLogger("memory")


def _ensure_memory_file(memory_path: Path) -> None:
    if memory_path.exists():
        return
    atomic_write_json(memory_path, zero_memory_payload())
    LOGGER.info("Created empty memory.json")


def _read_memory(memory_path: Path) -> Dict[str, Any]:
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
    data = _read_memory(memory_path)
    history: List[Dict[str, Any]] = list(data["action_history"])
    history.append(entry)
    if len(history) > max_entries:
        history = history[-max_entries:]
    data["action_history"] = history
    atomic_write_json(memory_path, data)
    LOGGER.debug("Memory entry added: %s", entry.get("command_id"))


def run_memory_loop(config: MemoryConfig, stop_event: Optional[threading.Event] = None) -> None:
    stop_event = stop_event or threading.Event()
    _ensure_memory_file(config.memory_path)

    last_command_id = ""
    LOGGER.info("Memory started command=%s memory=%s", config.command_path, config.memory_path)

    while not stop_event.is_set():
        raw_command = read_json(config.command_path)
        if not isinstance(raw_command, dict):
            stop_event.wait(MEMORY_POLL_WAIT_S)
            continue

        command_id = str(raw_command.get("command_id", ""))
        if not command_id or command_id == last_command_id:
            stop_event.wait(MEMORY_POLL_WAIT_S)
            continue

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
        action = str(raw_command.get("action", "LIGHT_OFF"))
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

    LOGGER.info("Memory stopped")


def get_recent_actions(memory_path: Path, limit: int = 8) -> List[Dict[str, Any]]:
    data = _read_memory(memory_path)
    history = data.get("action_history", [])
    if not isinstance(history, list):
        return []
    valid = [e for e in history if isinstance(e, dict)]
    return valid[-limit:]


def parse_args() -> MemoryConfig:
    return MemoryConfig()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    config = parse_args()
    try:
        run_memory_loop(config)
    except KeyboardInterrupt:
        LOGGER.info("Memory stopped by user")


if __name__ == "__main__":
    main()
