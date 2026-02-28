#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Модуль brain: читает `state.json`, принимает решение и пишет `command.json`.

Команды поворота разделены по углу:
- TURN_LEFT_15 / TURN_RIGHT_15 — малый поворот (~15°);
- TURN_LEFT_45 / TURN_RIGHT_45 — большой поворот (~45°).

Все параметры движений (speed, duration_ms) заданы в shared.ACTION_SPEED и ACTION_DURATION_MS.
command.json не содержит params.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from memory import get_recent_actions
from shared import ACTIONS, RobotCommand, RobotState, atomic_write_json, read_json

LOGGER = logging.getLogger("brain")
POLL_WAIT_S = 0.1

# Задача робота — можно менять для смены поведения
ROBOT_TASK = "Find and play with a toy"


def _json_line(payload) -> str:
    """Возвращает JSON с отступами для удобного чтения в консоли."""
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


@dataclass
class BrainConfig:
    """Настройки путей и порогов логики brain."""

    state_path: Path = Path(__file__).with_name("protocol") / "state.json"
    command_path: Path = Path(__file__).with_name("protocol") / "command.json"
    memory_path: Path = Path(__file__).with_name("protocol") / "memory.json"
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://192.168.0.18:11434")
    ollama_model: str = os.getenv("OLLAMA_BRAIN_MODEL", "qwen3.5:397b-cloud")
    ollama_timeout_s: float = float(os.getenv("OLLAMA_TIMEOUT_S", "100"))
    llm_temperature: float = 0.2
    llm_num_predict: int = 512
    llm_keep_alive: str = os.getenv("OLLAMA_KEEP_ALIVE", "60m")
    log_llm_verbose: bool = False


class BrainEngine:
    """Ядро принятия решений `state -> command`."""

    def __init__(self, config: BrainConfig) -> None:
        self.config = config
        self._counter = 0

    def _new_command(self, action: str, state_id: str, reason: str) -> RobotCommand:
        """Формирует команду с инкрементным id (параметры движения берутся из shared)."""
        self._counter += 1
        return RobotCommand(
            command_id=f"cmd_{self._counter:06d}",
            based_on_state_id=state_id,
            action=action,
            reason=reason,
        )

    def _build_llm_prompt(self, state: RobotState) -> str:
        """Формирует контекст состояния для LLM (sensor + metadata + recent_actions)."""
        payload: Dict[str, Any] = {
            "state_id": state.state_id,
            "sensor": state.sensor.to_dict(),
        }
        recent = get_recent_actions(self.config.memory_path, limit=8)
        if recent:
            payload["recent_actions"] = recent
        return json.dumps(payload, ensure_ascii=True, sort_keys=True)

    @staticmethod
    def _load_image_base64(image_path: Optional[str]) -> Optional[str]:
        """Читает изображение и возвращает base64-строку или None."""
        if not image_path:
            return None
        path = Path(image_path)
        if not path.exists() or not path.is_file():
            return None
        try:
            return base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError:
            return None

    @staticmethod
    def _system_prompt() -> str:
        allowed_actions = ", ".join(ACTIONS)
        return f"""You are a decision engine for a small (40x40 cm) robot.

**Task:** {ROBOT_TASK}.

You receive:
1. An image from the robot's front camera (what the robot sees ahead)
2. sensor.obstacle_cm — distance to the nearest obstacle in front, in cm (null if unavailable). Safe distance: >= 50 cm. Below 50 cm — be cautious (turn away, back up, or stop).
3. recent_actions — list of your last actions (state_id, action, reason, obstacle_cm). Use this to avoid repetitive loops.

Output ONLY a JSON object with keys: action, reason. Allowed action values: {allowed_actions}
Do not add markdown, comments, or extra keys.

## Commands

**Movement (drives a short distance then stops):**
- STEP_FORWARD — move forward
- STEP_BACKWARD — move backward

**Rotation (turns in place):**
- TURN_LEFT_15, TURN_RIGHT_15 — small correction (~15°)
- TURN_LEFT_45, TURN_RIGHT_45 — larger turn (~45°)

**Other:**
- STOP — stop and wait
- LIGHT_ON — turn light on
- LIGHT_OFF — turn light off

## Rules

1. **Safety and navigation:**
   - Combine the image and sensor.obstacle_cm to assess the situation. Obstacles in the image (wall, furniture, chair, legs, person) and low distance readings both suggest caution — turn away, back up, or stop as you see fit.
   - Obstacle on left → consider TURN_RIGHT. Obstacle on right → consider TURN_LEFT. Obstacle center → TURN_LEFT or TURN_RIGHT.

2. **Target seeking (when obstacle_cm >= 50 or null):**
   - Goal: keep the toy in the center of the image. If the toy is offset — turn towards it first. Do not drive forward past a toy that is off-center; you will miss it.
   - Toy on left side → TURN_LEFT_15 or TURN_LEFT_45 (turn until it moves toward center)
   - Toy at center → STEP_FORWARD
   - Toy on right side → TURN_RIGHT_15 or TURN_RIGHT_45 (turn until it moves toward center)
   - No toy visible → slow search turn (TURN_LEFT_15 or TURN_RIGHT_15) to find it

3. **Light:** If the image looks dark (low lighting, poorly lit room, shadows) — use LIGHT_ON to illuminate the scene. You can also use LIGHT_ON to draw attention to nearby toys. Use LIGHT_OFF when there is enough light. Avoid getting stuck in light-only loops (alternating LIGHT_ON/LIGHT_OFF repeatedly without movement).

4. **Use recent_actions:** You receive recent_actions (last actions taken). Use this history to avoid loops."""

    def _request_ollama(self, state: RobotState) -> Optional[Dict[str, Any]]:
        """Делает запрос к Ollama (vision model) и возвращает JSON-ответ."""
        started_at = time.perf_counter()

        def elapsed_s() -> float:
            return time.perf_counter() - started_at

        context = self._build_llm_prompt(state)  # includes recent_actions from memory
        images: List[str] = []
        image_b64 = self._load_image_base64(state.camera.image_path)
        if image_b64 is None and state.camera.image_path:
            LOGGER.warning("Brain: image_path=%r but failed to load", state.camera.image_path)
        if image_b64 is not None:
            images.append(image_b64)
            user_content = "Analyze this image and decide the robot action. Context: " + context
        else:
            user_content = "No image available. Decide based on sensor only (prefer STOP if unsure). Context: " + context

        user_message: Dict[str, Any] = {"role": "user", "content": user_content}
        if images:
            user_message["images"] = images

        url = self.config.ollama_base_url.rstrip("/") + "/api/chat"
        request_payload = {
            "model": self.config.ollama_model,
            "stream": False,
            "format": "json",
            "keep_alive": self.config.llm_keep_alive,
            "options": {
                "temperature": self.config.llm_temperature,
                "num_predict": self.config.llm_num_predict,
            },
            "messages": [
                {"role": "system", "content": self._system_prompt()},
                user_message,
            ],
        }
        body = json.dumps(request_payload).encode("utf-8")
        req = urllib.request.Request(url=url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.config.ollama_timeout_s) as response:
                raw = response.read().decode("utf-8", errors="replace")
            if self.config.log_llm_verbose:
                LOGGER.info("Brain LLM raw response: %s", raw)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            LOGGER.warning("Ollama request failed in %.3f s: %s", elapsed_s(), exc)
            return None

        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            LOGGER.warning("Ollama returned non-JSON payload in %.3f s", elapsed_s())
            return None
        if not isinstance(decoded, dict):
            LOGGER.warning("Ollama payload is not an object (%.3f s)", elapsed_s())
            return None

        message = decoded.get("message", {})
        if not isinstance(message, dict):
            LOGGER.warning("Ollama message field is invalid (%.3f s)", elapsed_s())
            return None
        content = message.get("content")
        if not isinstance(content, str):
            LOGGER.warning("Ollama content is missing (%.3f s)", elapsed_s())
            return None

        try:
            decision = json.loads(content)
        except json.JSONDecodeError:
            LOGGER.warning("LLM content is not a valid JSON decision (%.3f s)", elapsed_s())
            return None

        if not isinstance(decision, dict):
            LOGGER.warning("LLM decision is not an object (%.3f s)", elapsed_s())
            return None
        LOGGER.info("Ollama response time: %.3f s (model=%s)", elapsed_s(), self.config.ollama_model)
        return decision

    @staticmethod
    def _normalize_llm_decision(payload: Dict[str, Any]) -> Optional[Tuple[str, str]]:
        """Проверяет и нормализует решение LLM под контракт RobotCommand."""
        action = str(payload.get("action", "")).upper()
        if action not in ACTIONS:
            return None
        reason = str(payload.get("reason", "llm_decision")).strip() or "llm_decision"
        return action, reason

    def decide(self, state: Optional[RobotState]) -> RobotCommand:
        """Основная стратегия принятия решения для нового state."""
        if state is None:
            return self._new_command("STOP", "unknown", "state_missing")
        llm_raw = self._request_ollama(state)
        if llm_raw is None:
            return self._new_command("STOP", state.state_id, "llm_unavailable_fail_safe")

        normalized = self._normalize_llm_decision(llm_raw)
        if normalized is None:
            return self._new_command("STOP", state.state_id, "llm_invalid_response_fail_safe")

        action, reason = normalized
        return self._new_command(action, state.state_id, reason)


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

        LOGGER.info("Brain: deciding for state_id=%s (calling Ollama)", state.state_id)
        command = engine.decide(state)
        command_payload = command.to_dict()
        atomic_write_json(config.command_path, command_payload)
        LOGGER.info("COMMAND generated:\n%s", _json_line(command_payload))
        last_state_id = state.state_id

    LOGGER.info("Brain остановлен")


def parse_args() -> BrainConfig:
    parser = argparse.ArgumentParser(description="Brain module")
    parser.add_argument("--state-path", default=str(BrainConfig.state_path), help="Path to protocol/state.json")
    parser.add_argument("--command-path", default=str(BrainConfig.command_path), help="Path to protocol/command.json")
    parser.add_argument("--memory-path", default=str(BrainConfig.memory_path), help="Path to protocol/memory.json")
    parser.add_argument("--ollama-base-url", default=BrainConfig.ollama_base_url, help="Ollama URL, e.g. http://192.168.1.100:11434")
    parser.add_argument("--ollama-model", default=BrainConfig.ollama_model, help="Ollama model tag for brain")
    parser.add_argument("--ollama-timeout-s", type=float, default=BrainConfig.ollama_timeout_s, help="Timeout for Ollama requests in seconds")
    parser.add_argument("--llm-temperature", type=float, default=BrainConfig.llm_temperature, help="Sampling temperature for LLM")
    parser.add_argument("--llm-num-predict", type=int, default=BrainConfig.llm_num_predict, help="Max tokens predicted by LLM")
    parser.add_argument("--verbose", action="store_true", help="Логировать сырой ответ LLM")
    args = parser.parse_args()
    return BrainConfig(
        state_path=Path(args.state_path),
        command_path=Path(args.command_path),
        memory_path=Path(args.memory_path),
        ollama_base_url=str(args.ollama_base_url),
        ollama_model=str(args.ollama_model),
        ollama_timeout_s=max(0.1, float(args.ollama_timeout_s)),
        llm_temperature=max(0.0, float(args.llm_temperature)),
        llm_num_predict=max(1, int(args.llm_num_predict)),
        log_llm_verbose=args.verbose,
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
