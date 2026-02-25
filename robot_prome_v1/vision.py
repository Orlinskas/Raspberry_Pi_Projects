#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Модуль vision: читает датчики и публикует `state.json`.

По умолчанию работает в mock-режиме, чтобы можно было тестировать без железа.
"""

from __future__ import annotations

import argparse
import logging
import random
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol, Tuple

from shared import CameraState, ProximityState, RobotState, atomic_write_json, now_ts

LOGGER = logging.getLogger("vision")


class ProximitySensor(Protocol):
    """Интерфейс датчика расстояния."""

    def read_distance_cm(self) -> float:
        ...


class CameraDetector(Protocol):
    """Интерфейс обработки кадра с камеры."""

    def read_observation(self) -> Tuple[bool, Optional[float], float]:
        ...


class MockProximitySensor:
    """Генератор тестовой дистанции."""

    def read_distance_cm(self) -> float:
        return round(random.uniform(12.0, 120.0), 2)


class MockCameraDetector:
    """Генератор тестового результата камеры."""

    def read_observation(self) -> Tuple[bool, Optional[float], float]:
        obstacle = random.random() < 0.25
        target_x = round(random.uniform(-1.0, 1.0), 3) if not obstacle else None
        confidence = round(random.uniform(0.6, 0.95), 3) if obstacle else round(random.uniform(0.45, 0.9), 3)
        return obstacle, target_x, confidence


@dataclass
class VisionConfig:
    """Конфигурация цикла vision."""

    state_path: Path = Path(__file__).with_name("state.json")
    interval_s: float = 0.12
    use_mock: bool = True


def build_sensors(config: VisionConfig) -> Tuple[ProximitySensor, CameraDetector]:
    if config.use_mock:
        return MockProximitySensor(), MockCameraDetector()

    # В этом месте должны подключаться реальные адаптеры сенсоров.
    raise NotImplementedError("Реальные адаптеры сенсоров еще не подключены. Запустите без --real.")


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
        obstacle, target_x, confidence = camera.read_observation()
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
    LOGGER.info("Vision запущен. state_path=%s mock=%s", config.state_path, config.use_mock)

    while not stop_event.is_set():
        counter += 1
        state = _build_state(counter, proximity, camera)
        atomic_write_json(config.state_path, state.to_dict())
        LOGGER.debug("Опубликован state_id=%s", state.state_id)
        stop_event.wait(config.interval_s)

    LOGGER.info("Vision остановлен")


def parse_args() -> VisionConfig:
    parser = argparse.ArgumentParser(description="Vision module")
    parser.add_argument("--state-path", default=str(Path(__file__).with_name("state.json")))
    parser.add_argument("--interval", type=float, default=0.12)
    parser.add_argument("--real", action="store_true", help="Запуск с реальными сенсорами")
    args = parser.parse_args()
    return VisionConfig(
        state_path=Path(args.state_path),
        interval_s=max(0.03, float(args.interval)),
        use_mock=not bool(args.real),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    config = parse_args()
    try:
        run_vision_loop(config)
    except KeyboardInterrupt:
        LOGGER.info("Vision остановлен пользователем")


if __name__ == "__main__":
    main()
