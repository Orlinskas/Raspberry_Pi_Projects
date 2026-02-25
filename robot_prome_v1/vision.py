#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Модуль vision: читает датчики и публикует `state.json`.

По умолчанию работает в mock-режиме, чтобы можно было тестировать без железа.
"""

from __future__ import annotations

import argparse
import logging
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Optional, Protocol, Tuple

from shared import GPIO_LOCK, CameraState, FeelingsState, ProximityState, RobotState, atomic_write_json, now_ts, read_json

LOGGER = logging.getLogger("vision")
STATE_PATH = Path(__file__).with_name("state.json")
ECHO_PIN = 0
TRIG_PIN = 1
ULTRASONIC_TIMEOUT_S = 0.03
ULTRASONIC_MIN_CM = 2.0
ULTRASONIC_MAX_CM = 500.0

try:
    import RPi.GPIO as GPIO
except ImportError:  # pragma: no cover
    GPIO = None


class ProximitySensor(Protocol):
    """Интерфейс датчика расстояния."""

    def read_distance_cm(self) -> float:
        ...


class CameraDetector(Protocol):
    """Интерфейс обработки кадра с камеры."""

    def read_observation(self) -> Optional[Tuple[bool, Optional[float], float]]:
        ...


class UltrasonicProximitySensor:
    """Простой датчик HC-SR04: чтение + сглаживание по 3 последним замерам."""

    def __init__(self) -> None:
        self._initialized = False
        self._history: Deque[float] = deque(maxlen=3)

    def _init_gpio_once(self) -> None:
        if self._initialized:
            return
        if GPIO is None:
            raise RuntimeError("RPi.GPIO is unavailable")
        with GPIO_LOCK:
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            GPIO.setup(ECHO_PIN, GPIO.IN)
            GPIO.setup(TRIG_PIN, GPIO.OUT)
            GPIO.output(TRIG_PIN, GPIO.LOW)
        time.sleep(0.05)
        self._initialized = True

    def _read_once_cm(self) -> Optional[float]:
        deadline = time.monotonic() + ULTRASONIC_TIMEOUT_S
        with GPIO_LOCK:
            # Переустанавливаем направления при каждом чтении:
            # controller может делать GPIO.cleanup() при завершении.
            GPIO.setup(ECHO_PIN, GPIO.IN)
            GPIO.setup(TRIG_PIN, GPIO.OUT)

            GPIO.output(TRIG_PIN, GPIO.HIGH)
            time.sleep(0.000015)
            GPIO.output(TRIG_PIN, GPIO.LOW)

            while GPIO.input(ECHO_PIN) == 0:
                if time.monotonic() > deadline:
                    return None
            pulse_start = time.monotonic()

            while GPIO.input(ECHO_PIN) == 1:
                if time.monotonic() > deadline:
                    return None
            pulse_end = time.monotonic()

        distance_cm = (pulse_end - pulse_start) * 34300.0 / 2.0
        if ULTRASONIC_MIN_CM <= distance_cm <= ULTRASONIC_MAX_CM:
            return float(distance_cm)
        return None

    def read_distance_cm(self) -> float:
        self._init_gpio_once()
        distance = self._read_once_cm()
        if distance is None:
            if not self._history:
                raise RuntimeError("No valid ultrasonic echo")
            return float(statistics.mean(self._history))
        self._history.append(distance)
        return float(statistics.mean(self._history))


class MockCameraDetector:
    """Заглушка камеры: не подмешивает данные в решения brain."""

    def read_observation(self) -> Optional[Tuple[bool, Optional[float], float]]:
        return None


@dataclass
class VisionConfig:
    """Конфигурация цикла vision (только частота генерации state)."""

    interval_s: float = 3.0


def build_sensors(config: VisionConfig) -> Tuple[ProximitySensor, CameraDetector]:
    _ = config
    return UltrasonicProximitySensor(), MockCameraDetector()


def _build_state(state_counter: int, proximity: ProximitySensor, camera: CameraDetector) -> RobotState:
    """Формирует единый state из всех входов vision."""
    state_id = f"st_{state_counter:06d}"
    ts = now_ts()

    proximity_state = ProximityState(valid=False)
    camera_state = CameraState(valid=False)

    try:
        proximity_state = ProximityState(distance_cm=proximity.read_distance_cm(), valid=True)
    except Exception as exc:  # pragma: no cover
        LOGGER.warning("Ошибка чтения датчика приближения: %s", exc)

    try:
        observation = camera.read_observation()
        if observation is not None:
            obstacle, target_x, confidence = observation
            camera_state = CameraState(obstacle=obstacle, target_x=target_x, confidence=confidence, valid=True)
    except Exception as exc:  # pragma: no cover
        LOGGER.warning("Ошибка чтения камеры: %s", exc)

    return RobotState(
        state_id=state_id,
        timestamp=ts,
        proximity=proximity_state,
        camera=camera_state,
    )


def run_vision_loop(config: VisionConfig, stop_event: Optional[threading.Event] = None) -> None:
    """Основной цикл vision: publish state.json по таймеру."""
    stop_event = stop_event or threading.Event()
    proximity, camera = build_sensors(config)
    counter = 0
    LOGGER.info("Vision запущен. state_path=%s interval=%.2fs", STATE_PATH, config.interval_s)

    while not stop_event.is_set():
        counter += 1
        state = _build_state(counter, proximity, camera)
        current_state = read_json(STATE_PATH)
        if isinstance(current_state, dict):
            feelings_payload = current_state.get("feelings", {})
            if isinstance(feelings_payload, dict):
                state.feelings = FeelingsState.from_dict(feelings_payload)
        atomic_write_json(STATE_PATH, state.to_dict())
        LOGGER.debug("Опубликован state_id=%s", state.state_id)
        stop_event.wait(config.interval_s)

    LOGGER.info("Vision остановлен")


def parse_args() -> VisionConfig:
    parser = argparse.ArgumentParser(description="Vision module")
    parser.add_argument("--interval", type=float, default=3.0)
    args = parser.parse_args()
    return VisionConfig(interval_s=max(0.1, float(args.interval)))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    config = parse_args()
    try:
        run_vision_loop(config)
    except KeyboardInterrupt:
        LOGGER.info("Vision остановлен пользователем")


if __name__ == "__main__":
    main()
