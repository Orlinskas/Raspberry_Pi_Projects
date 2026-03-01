#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# Author:   Vlad Orlinskas
# Site:     https://prometeriy.com
# Project:  robot_prome_v1 — LLM-driven autonomous robot experiment
# License:  Free for any use
# ---------------------------------------------------------------------------

from __future__ import annotations

import argparse
import logging
import threading
import time
from pathlib import Path
from brain import BrainConfig, run_brain_loop
from controller import run_controller_loop
from memory import MemoryConfig, run_memory_loop
from settings import (
    CONTROLLER_POLL_INTERVAL_S,
    STREAM_DEFAULT_PORT,
    VisionConfig,
    atomic_write_json,
    read_json,
    zero_command_payload,
    zero_memory_payload,
    zero_state_payload,
)
from vision import run_vision_loop

LOGGER = logging.getLogger("main")


def monitor_health(
    state_path,
    command_path,
    stop_event: threading.Event,
    check_interval_s: float = 0.5,
) -> None:
    LOGGER.info("Health monitor started")
    while not stop_event.is_set():
        state = read_json(state_path)
        command = read_json(command_path)

        if not isinstance(state, dict):
            LOGGER.warning("state.json missing or corrupted")
        if not isinstance(command, dict):
            LOGGER.warning("command.json missing or corrupted")

        stop_event.wait(check_interval_s)
    LOGGER.info("Health monitor stopped")


def parse_args():
    parser = argparse.ArgumentParser(description="Robot main orchestrator")
    parser.add_argument("--mode", choices=["run", "dry"], default="run",
        help="run: motors enabled, dry: motors disabled")
    parser.add_argument("--verbose", action="store_true", help="Log raw LLM responses")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    args = parse_args()
    if args.mode == "dry":
        LOGGER.info("DRY mode: motors disabled")

    protocol_dir = Path(__file__).with_name("protocol")
    protocol_dir.mkdir(parents=True, exist_ok=True)
    state_path = protocol_dir / "state.json"
    command_path = protocol_dir / "command.json"
    memory_path = protocol_dir / "memory.json"

    atomic_write_json(command_path, zero_command_payload())
    atomic_write_json(state_path, zero_state_payload())
    atomic_write_json(memory_path, zero_memory_payload())

    stop_event = threading.Event()
    vision_config = VisionConfig(
        state_path=state_path,
        command_path=command_path,
        stream_port=STREAM_DEFAULT_PORT,
        stream_enabled=True,
    )
    brain_config = BrainConfig(
        state_path=state_path,
        command_path=command_path,
        memory_path=memory_path,
        log_llm_verbose=args.verbose,
    )
    memory_config = MemoryConfig(
        state_path=state_path,
        command_path=command_path,
        memory_path=memory_path,
    )

    threads = [
        threading.Thread(
            target=run_vision_loop,
            args=(vision_config, stop_event),
            name="vision",
            daemon=True
        ),
        threading.Thread(
            target=run_brain_loop,
            args=(brain_config, stop_event),
            name="brain",
            daemon=True
        ),
        threading.Thread(
            target=run_memory_loop,
            args=(memory_config, stop_event),
            name="memory",
            daemon=True)
        ,
        threading.Thread(
            target=run_controller_loop,
            args=(command_path, CONTROLLER_POLL_INTERVAL_S, stop_event, args.mode == "run"),
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
        LOGGER.info("Started thread: %s", thread.name)

    try:
        while True:
            dead = [thread.name for thread in threads if not thread.is_alive()]
            if dead:
                LOGGER.error("Critical modules dead: %s. Shutting down.", ", ".join(dead))
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        LOGGER.info("Stopped by user")
    finally:
        stop_event.set()
        for thread in threads:
            thread.join(timeout=3.0)
        atomic_write_json(state_path, zero_state_payload())
        atomic_write_json(command_path, zero_command_payload())
        atomic_write_json(memory_path, zero_memory_payload())
        LOGGER.info("Protocol files reset")
        LOGGER.info("Main orchestrator stopped")


if __name__ == "__main__":
    main()
