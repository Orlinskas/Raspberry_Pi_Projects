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
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from shared import ACTIONS, RobotCommand, RobotState, atomic_write_json, now_ts, read_json

LOGGER = logging.getLogger("brain")
POLL_WAIT_S = 0.1


def _json_line(payload) -> str:
    """Возвращает JSON с отступами для удобного чтения в консоли."""
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


@dataclass
class BrainConfig:
    """Настройки путей и порогов логики brain."""

    state_path: Path = Path(__file__).with_name("protocol") / "state.json"
    command_path: Path = Path(__file__).with_name("protocol") / "command.json"
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://192.168.0.18:11434")
    ollama_model: str = os.getenv("OLLAMA_BRAIN_MODEL", "gemma3")
    ollama_timeout_s: float = float(os.getenv("OLLAMA_TIMEOUT_S", "100"))
    llm_temperature: float = 0.1
    llm_num_predict: int = 256
    llm_keep_alive: str = os.getenv("OLLAMA_KEEP_ALIVE", "60m")


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
            timestamp=now_ts(),
            based_on_state_id=state_id,
            action=action,
            reason=reason,
        )

    @staticmethod
    def _build_llm_prompt(state: RobotState) -> str:
        """Формирует минимальный контекст состояния для LLM."""
        payload = {
            "state_id": state.state_id,
            "timestamp": state.timestamp,
            "sensor": state.sensor.to_dict(),
            "camera": state.camera.to_dict(),
        }
        return json.dumps(payload, ensure_ascii=True, sort_keys=True)

    @staticmethod
    def _system_prompt() -> str:
        allowed_actions = ", ".join(ACTIONS)
        return f"""## Description

You are a decision engine for a mobile robot. The robot is small and curious; it should approach toys when safe.

**Input:** State JSON with:
- sensor.obstacle_cm — front distance in cm (null if unavailable)
- camera.grid — 5x5 array of strings: '.' free, 'R' robot (center row 2 col 2), 'O' obstacle, 'T' target. Row 0=top, row 4=bottom, col 0=left, col 4=right.
- camera.description — short scene summary (or null)
- camera.target_x — horizontal offset of target -1..1 (null if none)

## Task

Output ONLY a JSON object with keys: action, reason.
Allowed action values: {allowed_actions}
Do not add markdown, comments, or extra keys.

## Commands

**Movement (drives a short distance then stops by itself):**
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

1. **Safety first (strict priority):**
   - If sensor.obstacle_cm is not null and <= 50: avoid obstacle (TURN_LEFT_15, TURN_LEFT_45, TURN_RIGHT_15, TURN_RIGHT_45, or STEP_BACKWARD)
   - If grid shows obstacle 'O' in front sector (rows 0–1, cols 1–3): avoid

2. **Target seeking (when safe):**
   - If camera.description mentions toy-like object (toy, ball, teddy): prefer moving toward it
   - Infer target position from grid: 'T' in left (cols 0–1) → turn left; right (cols 3–4) → turn right; center (col 2) → STEP_FORWARD
   - Use camera.target_x if present; else infer from grid (where is 'T' relative to center)
   - Target left of center → TURN_LEFT_15 or TURN_LEFT_45 (use 45 for large offset)
   - Target at center → STEP_FORWARD
   - Target right of center → TURN_RIGHT_15 or TURN_RIGHT_45 (use 45 for large offset)
   - No target visible → slow search turn (TURN_LEFT_15 or TURN_RIGHT_15)

3. **Light:** Enable light in dark rooms. Blink at nearby toys (LIGHT_ON, LIGHT_OFF) but do not get stuck in light-only loops.

4. **Avoid excessive turns:** If state is safe and target is near center (grid col 2 or target_x in [-0.2,0.2]), prefer STEP_FORWARD."""

    def _request_ollama(self, state: RobotState) -> Optional[Dict[str, Any]]:
        """Делает запрос к Ollama и возвращает JSON-ответ модели."""
        started_at = time.perf_counter()

        def elapsed_s() -> float:
            return time.perf_counter() - started_at

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
                {"role": "user", "content": self._build_llm_prompt(state)},
            ],
        }
        body = json.dumps(request_payload).encode("utf-8")
        req = urllib.request.Request(url=url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.config.ollama_timeout_s) as response:
                raw = response.read().decode("utf-8", errors="replace")
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

        LOGGER.info("STATE used:\n%s", _json_line(raw_state))
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
    parser.add_argument("--ollama-base-url", default=BrainConfig.ollama_base_url, help="Ollama URL, e.g. http://192.168.1.100:11434")
    parser.add_argument("--ollama-model", default=BrainConfig.ollama_model, help="Ollama model tag for brain")
    parser.add_argument("--ollama-timeout-s", type=float, default=BrainConfig.ollama_timeout_s, help="Timeout for Ollama requests in seconds")
    parser.add_argument("--llm-temperature", type=float, default=BrainConfig.llm_temperature, help="Sampling temperature for LLM")
    parser.add_argument("--llm-num-predict", type=int, default=BrainConfig.llm_num_predict, help="Max tokens predicted by LLM")
    args = parser.parse_args()
    return BrainConfig(
        state_path=Path(args.state_path),
        command_path=Path(args.command_path),
        ollama_base_url=str(args.ollama_base_url),
        ollama_model=str(args.ollama_model),
        ollama_timeout_s=max(0.1, float(args.ollama_timeout_s)),
        llm_temperature=max(0.0, float(args.llm_temperature)),
        llm_num_predict=max(1, int(args.llm_num_predict)),
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
