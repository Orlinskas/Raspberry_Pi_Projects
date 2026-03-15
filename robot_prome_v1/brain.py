#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# Author:   Vlad Orlinskas
# Site:     https://prometeriy.com
# Project:  robot_prome_v1 — LLM-driven autonomous robot experiment
# License:  Free for any use
# ---------------------------------------------------------------------------

from __future__ import annotations

import argparse
import base64
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from memory import get_recent_actions
from settings import (
    ACTIONS,
    BRAIN_POLL_WAIT_S,
    BrainConfig,
    RobotCommand,
    RobotState,
    atomic_write_json,
    get_brain_system_prompt,
    read_json,
)

LOGGER = logging.getLogger("brain")


def _json_line(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


class BrainEngine:
    def __init__(self, config: BrainConfig) -> None:
        self.config = config
        self._counter = 0

    def _new_command(
        self,
        action: str,
        state_id: str,
        reason: str,
        voice: Optional[str] = None,
    ) -> RobotCommand:
        self._counter += 1
        return RobotCommand(
            command_id=f"cmd_{self._counter:06d}",
            based_on_state_id=state_id,
            action=action,
            reason=reason,
            voice=voice,
        )

    def _build_llm_prompt(self, state: RobotState) -> str:
        payload: Dict[str, Any] = {
            "state_id": state.state_id,
            "sensor": state.sensor.to_dict(),
            "command": state.command,
        }
        recent = get_recent_actions(self.config.memory_path, limit=8)
        if recent:
            payload["recent_actions"] = recent
        return json.dumps(payload, ensure_ascii=True, sort_keys=True)

    @staticmethod
    def _load_image_base64(image_path: Optional[str]) -> Optional[str]:
        if not image_path:
            return None
        path = Path(image_path)
        if not path.exists() or not path.is_file():
            return None
        try:
            return base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError:
            return None

    def _request_ollama(self, state: RobotState) -> Optional[Dict[str, Any]]:
        started_at = time.perf_counter()

        def elapsed_s() -> float:
            return time.perf_counter() - started_at

        context = self._build_llm_prompt(state)
        images: List[str] = []
        image_b64 = self._load_image_base64(state.camera.image_path)
        if image_b64 is None and state.camera.image_path:
            LOGGER.warning("Brain: image load failed path=%r", state.camera.image_path)
        if image_b64 is not None:
            images.append(image_b64)
            user_content = "Analyze this image and decide the robot action. Context: " + context
        else:
            user_content = "No image available. Decide based on sensor only. Context: " + context

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
                {"role": "system", "content": get_brain_system_prompt()},
                user_message,
            ],
        }
        body = json.dumps(request_payload).encode("utf-8")
        req = urllib.request.Request(url=url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.config.ollama_timeout_s) as response:
                raw = response.read().decode("utf-8", errors="replace")
            if self.config.log_llm_verbose:
                LOGGER.info("Brain LLM raw: %s", raw)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            LOGGER.warning("Ollama request failed %.3fs: %s", elapsed_s(), exc)
            return None

        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            LOGGER.warning("Ollama non-JSON response %.3fs", elapsed_s())
            return None
        if not isinstance(decoded, dict):
            LOGGER.warning("Ollama payload not object %.3fs", elapsed_s())
            return None

        message = decoded.get("message", {})
        if not isinstance(message, dict):
            LOGGER.warning("Ollama message invalid %.3fs", elapsed_s())
            return None
        content = message.get("content")
        if not isinstance(content, str):
            LOGGER.warning("Ollama content missing %.3fs", elapsed_s())
            return None

        try:
            decision = json.loads(content)
        except json.JSONDecodeError:
            LOGGER.warning("LLM invalid JSON %.3fs", elapsed_s())
            return None

        if not isinstance(decision, dict):
            LOGGER.warning("LLM decision not object %.3fs", elapsed_s())
            return None
        LOGGER.info("Ollama %.3fs model=%s", elapsed_s(), self.config.ollama_model)
        return decision

    def clear_consumed_command(self, state: RobotState) -> None:
        consumed = (state.command or "").strip()
        if not consumed:
            return
        latest_state = read_json(self.config.state_path)
        if not isinstance(latest_state, dict):
            return

        # Avoid overwriting newer state written by vision.
        if str(latest_state.get("state_id", "")) != state.state_id:
            return
        if str(latest_state.get("command", "")).strip() != consumed:
            return

        latest_state["command"] = ""
        atomic_write_json(self.config.state_path, latest_state)
        LOGGER.info("Brain consumed and cleared state.command for state_id=%s", state.state_id)

    @staticmethod
    def _normalize_llm_decision(payload: Dict[str, Any]) -> Optional[Tuple[str, str, Optional[str]]]:
        action = str(payload.get("action", "")).upper()
        if action not in ACTIONS:
            return None
        reason = str(payload.get("reason", "llm_decision")).strip() or "llm_decision"
        voice_raw = payload.get("voice")
        voice = str(voice_raw).strip() if voice_raw is not None else ""
        voice = voice or None
        return action, reason, voice

    def decide(self, state: Optional[RobotState]) -> RobotCommand:
        if state is None:
            return self._new_command("ERROR", "unknown", "state_missing")
        llm_raw = self._request_ollama(state)
        if llm_raw is None:
            return self._new_command("ERROR", state.state_id, "llm_unavailable_fail_safe")

        normalized = self._normalize_llm_decision(llm_raw)
        if normalized is None:
            return self._new_command("ERROR", state.state_id, "llm_invalid_response_fail_safe")

        action, reason, voice = normalized
        return self._new_command(action, state.state_id, reason, voice)


def run_brain_loop(config: BrainConfig, stop_event: Optional[threading.Event] = None) -> None:
    stop_event = stop_event or threading.Event()
    engine = BrainEngine(config)
    LOGGER.info("Brain started state=%s command=%s", config.state_path, config.command_path)
    last_state_id = ""

    while not stop_event.is_set():
        raw_state = read_json(config.state_path)
        if not isinstance(raw_state, dict):
            stop_event.wait(BRAIN_POLL_WAIT_S)
            continue

        state = RobotState.from_dict(raw_state)
        if not state.state_id:
            stop_event.wait(BRAIN_POLL_WAIT_S)
            continue

        if state.state_id == last_state_id:
            stop_event.wait(BRAIN_POLL_WAIT_S)
            continue

        LOGGER.info("Brain deciding state_id=%s", state.state_id)
        command = engine.decide(state)
        engine.clear_consumed_command(state)
        command_payload = command.to_dict()
        atomic_write_json(config.command_path, command_payload)
        LOGGER.info("COMMAND generated:\n%s", _json_line(command_payload))
        last_state_id = state.state_id

    LOGGER.info("Brain stopped")


def parse_args() -> BrainConfig:
    parser = argparse.ArgumentParser(description="Brain module")
    parser.add_argument("--verbose", action="store_true", help="Log raw LLM responses")
    args = parser.parse_args()
    return BrainConfig(log_llm_verbose=args.verbose)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    config = parse_args()
    try:
        run_brain_loop(config)
    except KeyboardInterrupt:
        LOGGER.info("Brain stopped by user")


if __name__ == "__main__":
    main()
