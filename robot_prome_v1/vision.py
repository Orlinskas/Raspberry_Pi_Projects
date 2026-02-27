#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Модуль vision: читает датчики и публикует `protocol/state.json`."""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import socket
import statistics
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from pathlib import Path
from typing import Any, Deque, Dict, Optional, Protocol, Tuple

from shared import (
    DEFAULT_GRID_5X5,
    GPIO_LOCK,
    CameraState,
    ProximityState,
    RobotState,
    atomic_write_json,
    now_ts,
    read_json,
)

LOGGER = logging.getLogger("vision")
STATE_PATH = Path(__file__).with_name("protocol") / "state.json"
CAPTURE_DIR = Path(__file__).with_name("captures")

ECHO_PIN = 0
TRIG_PIN = 1
ULTRASONIC_TIMEOUT_S = 0.03
ULTRASONIC_MIN_CM = 2.0
ULTRASONIC_MAX_CM = 500.0
# HC-SR04: минимум 60 мс между измерениями (документация) — иначе эхо от прошлого цикла
ULTRASONIC_INTER_MEASURE_DELAY_S = 0.06
# Количество замеров за один вызов read_distance_cm; медиана отсекает выбросы
ULTRASONIC_SAMPLES_PER_READ = 5
# Отклонение от медианы > этой доли — считаем выбросом (0.4 = 40%)
ULTRASONIC_OUTLIER_RATIO = 0.4

CAMERA_INDEX = 0
CAMERA_WIDTH = 640
CAMERA_HEIGHT = 480
CAMERA_FPS = 30.0
CAMERA_WARMUP_S = 1.0
CAPTURE_KEEP_LAST = 30

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://192.168.0.18:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_VISION_MODEL", "gemma3")
OLLAMA_TIMEOUT_S = float(os.getenv("OLLAMA_TIMEOUT_S", "100"))
OLLAMA_TEMPERATURE = 0.1
OLLAMA_NUM_PREDICT = 256
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "60m")

try:
    import RPi.GPIO as GPIO
except ImportError:  # pragma: no cover
    GPIO = None

try:
    import cv2
except ImportError:  # pragma: no cover
    cv2 = None

# Порт для MJPEG-потока камеры в браузере
STREAM_DEFAULT_PORT = 8765


class FrameBuffer:
    """Потокобезопасный буфер последнего кадра с камеры для веб-стрима."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame: Optional[bytes] = None  # JPEG-байты

    def put(self, jpeg_bytes: bytes) -> None:
        with self._lock:
            self._frame = bytes(jpeg_bytes)

    def get(self) -> Optional[bytes]:
        with self._lock:
            return self._frame if self._frame is not None else None


@dataclass
class CameraObservation:
    """Нормализованный результат camera + vision модели."""

    grid: list
    description: Optional[str]
    target_x: Optional[float]


class ProximitySensor(Protocol):
    """Интерфейс датчика расстояния."""

    def read_distance_cm(self) -> float:
        ...


class CameraDetector(Protocol):
    """Интерфейс обработки кадра с камеры."""

    def read_observation(self, state_id: str) -> Optional[CameraObservation]:
        ...

    def get_last_image_path(self) -> Optional[str]:
        ...

    def close(self) -> None:
        ...


class UltrasonicProximitySensor:
    """Датчик HC-SR04: несколько замеров, отсечение выбросов, медиана, задержка 60 мс."""

    def __init__(self) -> None:
        self._initialized = False
        self._history: Deque[float] = deque(maxlen=5)
        self._last_read_time: float = 0.0

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
            GPIO.setup(ECHO_PIN, GPIO.IN)
            GPIO.setup(TRIG_PIN, GPIO.OUT)
            GPIO.output(TRIG_PIN, GPIO.LOW)
            time.sleep(0.000002)
            GPIO.output(TRIG_PIN, GPIO.HIGH)
            time.sleep(0.00001)
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

    def _filter_outliers(self, samples: list[float]) -> list[float]:
        """Отбрасывает замеры, сильно отличающиеся от медианы."""
        if len(samples) < 2:
            return samples
        med = statistics.median(samples)
        # Для близких расстояний — абсолютный порог 5 см, для дальних — относительный
        threshold = max(5.0, med * ULTRASONIC_OUTLIER_RATIO)
        return [s for s in samples if abs(s - med) <= threshold]

    def read_distance_cm(self) -> float:
        self._init_gpio_once()
        now = time.monotonic()
        elapsed = now - self._last_read_time
        if elapsed < ULTRASONIC_INTER_MEASURE_DELAY_S and self._history:
            return float(self._history[-1])
        samples: list[float] = []
        for _ in range(ULTRASONIC_SAMPLES_PER_READ):
            d = self._read_once_cm()
            if d is not None:
                samples.append(d)
            time.sleep(ULTRASONIC_INTER_MEASURE_DELAY_S)
        self._last_read_time = time.monotonic()
        valid = self._filter_outliers(samples)
        if not valid:
            if not self._history:
                raise RuntimeError("No valid ultrasonic echo")
            return float(statistics.median(self._history))
        result = statistics.median(valid)
        self._history.append(result)
        return result


class MockCameraDetector:
    """Заглушка камеры: не подмешивает данные в решения brain."""

    def read_observation(self, state_id: str) -> Optional[CameraObservation]:
        _ = state_id
        return None

    def get_last_image_path(self) -> Optional[str]:
        return None

    def close(self) -> None:
        return None


class OpenCVCameraDetector:
    """One-shot захват изображения с USB-камеры."""

    def __init__(
        self,
        capture_dir: Path,
        camera_index: int = CAMERA_INDEX,
        width: int = CAMERA_WIDTH,
        height: int = CAMERA_HEIGHT,
        fps: float = CAMERA_FPS,
        keep_last: int = CAPTURE_KEEP_LAST,
        ollama_base_url: str = OLLAMA_BASE_URL,
        ollama_model: str = OLLAMA_MODEL,
        ollama_timeout_s: float = OLLAMA_TIMEOUT_S,
        ollama_temperature: float = OLLAMA_TEMPERATURE,
        ollama_num_predict: int = OLLAMA_NUM_PREDICT,
        ollama_keep_alive: str = OLLAMA_KEEP_ALIVE,
        frame_buffer: Optional[FrameBuffer] = None,
    ) -> None:
        self._capture_dir = capture_dir
        self._frame_buffer = frame_buffer
        self._camera_index = camera_index
        self._width = width
        self._height = height
        self._fps = fps
        self._keep_last = max(1, int(keep_last))
        self._ollama_base_url = str(ollama_base_url).rstrip("/")
        self._ollama_model = str(ollama_model)
        self._ollama_timeout_s = max(0.1, float(ollama_timeout_s))
        self._ollama_temperature = max(0.0, float(ollama_temperature))
        self._ollama_num_predict = max(1, int(ollama_num_predict))
        self._ollama_keep_alive = str(ollama_keep_alive)
        self._cap = None
        self._last_image_path: Optional[str] = None
        self._open_warning_logged = False

    def _ensure_open(self) -> bool:
        if cv2 is None:
            if not self._open_warning_logged:
                LOGGER.warning("OpenCV (cv2) недоступен, камера отключена")
                self._open_warning_logged = True
            return False

        if self._cap is not None and self._cap.isOpened():
            return True

        self.close()
        cap = cv2.VideoCapture(self._camera_index)
        if not cap.isOpened():
            cap.release()
            if not self._open_warning_logged:
                LOGGER.warning("Не удалось открыть USB-камеру index=%s", self._camera_index)
                self._open_warning_logged = True
            return False

        cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(self._width))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self._height))
        cap.set(cv2.CAP_PROP_FPS, float(self._fps))
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter.fourcc("M", "J", "P", "G"))
        time.sleep(CAMERA_WARMUP_S)
        self._cap = cap
        self._open_warning_logged = False
        return True

    @staticmethod
    def _vision_system_prompt() -> str:
        return (
            "You are a camera perception engine for a SMALL mobile robot on the FLOOR. "
            "Return ONLY one JSON object with keys: grid, description, target_x. "
            "grid must be array of 5 strings, each length 5. Symbols: '.' free, 'R' robot, 'O' obstacle, 'T' target. "
            "Robot is always at center: row 2, col 2 (index 0-based). "
            "ALWAYS mark as obstacle 'O': walls, chairs, tables, furniture, legs, door frames, corners, any solid object the robot might hit. "
            "Be conservative: if unsure whether something blocks the path, mark it as 'O'. "
            "description: short scene summary (5-15 words) or null. "
            "target_x: number -1.0..1.0 (-1 left, 0 center, 1 right) or null. "
            "No markdown, no extra keys."
        )

    @staticmethod
    def _vision_user_prompt(state_id: str) -> str:
        payload = {
            "state_id": state_id,
            "task": (
                "Build a 5x5 top-down grid. Robot at center (row 2 col 2). "
                "Use O for ALL obstacles: chairs, walls, furniture, table legs, doors, anything solid. "
                "Use . for free floor. Use T for target (toy, ball). "
                "Mark chairs and walls as O whenever visible — the robot must avoid them. "
                "Return grid (5 strings of 5 chars), description, target_x."
            ),
        }
        return json.dumps(payload, ensure_ascii=True, sort_keys=True)

    def _request_ollama(self, image_path: Path, state_id: str) -> Optional[Dict[str, Any]]:
        started_at = time.perf_counter()

        def elapsed_s() -> float:
            return time.perf_counter() - started_at

        try:
            image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        except OSError as exc:
            LOGGER.warning("Не удалось прочитать снимок для Ollama: %s", exc)
            return None

        request_payload = {
            "model": self._ollama_model,
            "stream": False,
            "format": "json",
            "keep_alive": self._ollama_keep_alive,
            "options": {
                "temperature": self._ollama_temperature,
                "num_predict": self._ollama_num_predict,
            },
            "messages": [
                {"role": "system", "content": self._vision_system_prompt()},
                {
                    "role": "user",
                    "content": self._vision_user_prompt(state_id),
                    "images": [image_b64],
                },
            ],
        }
        body = json.dumps(request_payload).encode("utf-8")
        req = urllib.request.Request(
            url=self._ollama_base_url + "/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._ollama_timeout_s) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            LOGGER.warning("Vision Ollama request failed in %.3f s (state_id=%s): %s", elapsed_s(), state_id, exc)
            return None

        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            LOGGER.warning("Vision Ollama returned non-JSON payload in %.3f s (state_id=%s)", elapsed_s(), state_id)
            return None
        if not isinstance(decoded, dict):
            return None

        message = decoded.get("message", {})
        if not isinstance(message, dict):
            return None
        content = message.get("content")
        if not isinstance(content, str):
            return None

        json_text = self._extract_json_object_text(content)
        try:
            parsed = json.loads(json_text)
        except json.JSONDecodeError:
            preview = content.replace("\n", " ")[:220]
            LOGGER.warning(
                "Vision model content is not valid JSON in %.3f s (state_id=%s preview=%r)",
                elapsed_s(),
                state_id,
                preview,
            )
            return None
        if not isinstance(parsed, dict):
            return None
        LOGGER.info("Vision Ollama response time: %.3f s (model=%s state_id=%s)", elapsed_s(), self._ollama_model, state_id)
        return parsed

    @staticmethod
    def _extract_json_object_text(content: str) -> str:
        """Достаёт JSON-объект из текста ответа модели."""
        raw = content.strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, flags=re.DOTALL | re.IGNORECASE)
        if fenced:
            return fenced.group(1).strip()
        first = raw.find("{")
        last = raw.rfind("}")
        if first != -1 and last != -1 and last > first:
            return raw[first : last + 1]
        return raw

    @staticmethod
    def _build_grid_5x5(
        obstacles: list[Dict[str, Any]],
        target: Optional[Dict[str, Any]],
    ) -> list[str]:
        grid = [["." for _ in range(5)] for _ in range(5)]
        robot_row, robot_col = 2, 2
        grid[robot_row][robot_col] = "R"

        for obstacle in obstacles:
            try:
                col = int(round(float(obstacle["x"]) * 4.0))
                row = int(round(float(obstacle["y"]) * 4.0))
            except (KeyError, TypeError, ValueError):
                continue
            row = max(0, min(4, row))
            col = max(0, min(4, col))
            if row == robot_row and col == robot_col:
                continue
            grid[row][col] = "O"

        if isinstance(target, dict):
            try:
                t_col = int(round(float(target["x"]) * 4.0))
                t_row = int(round(float(target["y"]) * 4.0))
            except (KeyError, TypeError, ValueError):
                t_row, t_col = -1, -1
            if t_row >= 0 and t_col >= 0:
                t_row = max(0, min(4, t_row))
                t_col = max(0, min(4, t_col))
                if not (t_row == robot_row and t_col == robot_col):
                    grid[t_row][t_col] = "T"

        return ["".join(row_cells) for row_cells in grid]

    @staticmethod
    def _empty_grid_5x5() -> list:
        return list(DEFAULT_GRID_5X5)

    @staticmethod
    def _extract_or_build_grid(payload: Dict[str, Any], target_x: Optional[float]) -> list:
        """Extract grid from payload or build from obstacles/target."""
        grid_raw = payload.get("grid")
        if isinstance(grid_raw, list) and len(grid_raw) == 5:
            valid = all(isinstance(row, str) and len(row) == 5 for row in grid_raw)
            if valid:
                return list(grid_raw)

        scene_map = payload.get("scene_map")
        obstacles: list[Dict[str, Any]] = []
        target: Optional[Dict[str, Any]] = None
        if isinstance(scene_map, dict):
            obstacles_raw = scene_map.get("obstacles")
            if isinstance(obstacles_raw, list):
                for item in obstacles_raw:
                    if not isinstance(item, dict):
                        continue
                    x = item.get("x")
                    y = item.get("y")
                    try:
                        x, y = float(x), float(y)
                        obstacles.append({"x": max(0.0, min(1.0, x)), "y": max(0.0, min(1.0, y))})
                    except (TypeError, ValueError):
                        continue
            target_raw = scene_map.get("target")
            if isinstance(target_raw, dict):
                try:
                    tx = float(target_raw.get("x", 0))
                    ty = float(target_raw.get("y", 0))
                    target = {"x": max(0.0, min(1.0, tx)), "y": max(0.0, min(1.0, ty))}
                except (TypeError, ValueError):
                    target = None
            elif target_x is not None:
                target = {"x": (target_x + 1.0) / 2.0, "y": 0.35}

        return OpenCVCameraDetector._build_grid_5x5(obstacles=obstacles, target=target)

    @staticmethod
    def _normalize_vision_payload(payload: Dict[str, Any]) -> CameraObservation:
        description = payload.get("description")
        if description is not None:
            description = str(description).strip() or None

        target_x = payload.get("target_x")
        try:
            target_x = float(target_x) if target_x is not None else None
        except (TypeError, ValueError):
            target_x = None
        if target_x is not None:
            target_x = max(-1.0, min(1.0, target_x))
        grid = OpenCVCameraDetector._extract_or_build_grid(payload, target_x=target_x)

        return CameraObservation(
            grid=grid,
            description=description,
            target_x=target_x,
        )

    def read_observation(self, state_id: str) -> Optional[CameraObservation]:
        self._last_image_path = None
        if not self._ensure_open():
            return None

        assert self._cap is not None  # for type checkers
        ok, frame = self._cap.read()
        if not ok or frame is None:
            LOGGER.warning("Не удалось получить кадр из USB-камеры")
            return None

        self._capture_dir.mkdir(parents=True, exist_ok=True)
        image_path = self._capture_dir / f"{state_id}.jpg"
        if not cv2.imwrite(str(image_path), frame):
            LOGGER.warning("Не удалось сохранить кадр: %s", image_path)
            return None

        # Обновляем буфер для веб-стрима (не блокирует работу робота)
        if self._frame_buffer is not None and cv2 is not None:
            _, jpeg = cv2.imencode(".jpg", frame)
            if jpeg is not None:
                self._frame_buffer.put(jpeg.tobytes())

        _prune_capture_images(self._capture_dir, keep_last=self._keep_last)
        self._last_image_path = str(image_path.resolve())
        model_payload = self._request_ollama(
            image_path=image_path,
            state_id=state_id,
        )
        if model_payload is None:
            return CameraObservation(
                grid=OpenCVCameraDetector._empty_grid_5x5(),
                description=None,
                target_x=None,
            )
        return self._normalize_vision_payload(model_payload)

    def get_last_image_path(self) -> Optional[str]:
        return self._last_image_path

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None


def _get_local_ip() -> str:
    """Возвращает локальный IP для отображения в инструкциях."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _make_stream_handler(frame_buffer: FrameBuffer) -> type:
    """Создаёт класс HTTP-обработчика с замыканием на frame_buffer."""

    class MJPEGStreamHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            LOGGER.debug("Stream %s", args[0] if args else "")

        def do_GET(self) -> None:
            if self.path == "/" or self.path == "":
                self._serve_index()
            elif self.path == "/stream":
                self._serve_mjpeg()
            else:
                self.send_error(404)

        def _serve_index(self) -> None:
            html = (
                b"<!DOCTYPE html><html><head><meta charset='utf-8'>"
                b"<title>Robot Camera</title></head><body style='margin:0;background:#111'>"
                b"<img src='/stream' style='display:block;max-width:100%;height:auto'>"
                b"</body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def _serve_mjpeg(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            while True:
                frame = frame_buffer.get()
                if frame:
                    try:
                        part = (
                            b"--frame\r\n"
                            b"Content-Type: image/jpeg\r\n"
                            b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                        )
                        self.wfile.write(part)
                        self.wfile.write(frame)
                        self.wfile.write(b"\r\n")
                    except (BrokenPipeError, ConnectionResetError):
                        break
                time.sleep(0.1)

    return MJPEGStreamHandler


class _ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def run_stream_server(
    port: int,
    frame_buffer: FrameBuffer,
    stop_event: threading.Event,
) -> None:
    """Запускает HTTP-сервер MJPEG-потока в фоновом потоке (daemon)."""
    handler = _make_stream_handler(frame_buffer)
    server = _ThreadedHTTPServer(("0.0.0.0", port), handler)

    def serve() -> None:
        def shutdown_on_stop() -> None:
            stop_event.wait()
            server.shutdown()

        t = threading.Thread(target=shutdown_on_stop, daemon=True)
        t.start()
        server.serve_forever()

    thread = threading.Thread(target=serve, name="camera-stream", daemon=True)
    thread.start()
    LOGGER.info(
        "Video stream: http://%s:%d  (or http://127.0.0.1:%d)",
        _get_local_ip(),
        port,
        port,
    )


@dataclass
class VisionConfig:
    """Конфигурация синхронного vision-цикла."""

    capture_dir: Path = CAPTURE_DIR
    capture_keep_last: int = CAPTURE_KEEP_LAST
    stream_port: int = STREAM_DEFAULT_PORT
    stream_enabled: bool = True
    ollama_base_url: str = OLLAMA_BASE_URL
    ollama_model: str = OLLAMA_MODEL
    ollama_timeout_s: float = OLLAMA_TIMEOUT_S
    ollama_temperature: float = OLLAMA_TEMPERATURE
    ollama_num_predict: int = OLLAMA_NUM_PREDICT
    ollama_keep_alive: str = OLLAMA_KEEP_ALIVE


def build_sensors(
    config: VisionConfig,
    frame_buffer: Optional[FrameBuffer] = None,
) -> Tuple[ProximitySensor, CameraDetector]:
    if cv2 is None:
        LOGGER.error("cv2 не найден, используется MockCameraDetector")
        camera: CameraDetector = MockCameraDetector()
    else:
        camera = OpenCVCameraDetector(
            capture_dir=config.capture_dir,
            keep_last=config.capture_keep_last,
            ollama_base_url=config.ollama_base_url,
            ollama_model=config.ollama_model,
            ollama_timeout_s=config.ollama_timeout_s,
            ollama_temperature=config.ollama_temperature,
            ollama_num_predict=config.ollama_num_predict,
            ollama_keep_alive=config.ollama_keep_alive,
            frame_buffer=frame_buffer,
        )
    return UltrasonicProximitySensor(), camera


def _clear_capture_images(capture_dir: Path) -> None:
    """Очищает каталог снимков перед запуском vision."""
    if not capture_dir.exists():
        capture_dir.mkdir(parents=True, exist_ok=True)
        return
    deleted = 0
    for path in capture_dir.iterdir():
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
            continue
        try:
            path.unlink()
            deleted += 1
        except OSError as exc:  # pragma: no cover
            LOGGER.warning("Не удалось удалить старый снимок %s: %s", path, exc)
    if deleted:
        LOGGER.info("Очищен каталог снимков: удалено %s файлов", deleted)


def _prune_capture_images(capture_dir: Path, keep_last: int) -> None:
    """Хранит только последние keep_last снимков в каталоге."""
    keep_last = max(1, int(keep_last))
    files = [
        path
        for path in capture_dir.iterdir()
        if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png"}
    ]
    if len(files) <= keep_last:
        return
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for old_path in files[keep_last:]:
        try:
            old_path.unlink()
        except OSError as exc:  # pragma: no cover
            LOGGER.warning("Не удалось удалить старый снимок %s: %s", old_path, exc)


def _build_state(state_counter: int, proximity: ProximitySensor, camera: CameraDetector) -> RobotState:
    """Формирует единый state из всех входов vision."""
    state_id = f"st_{state_counter:06d}"
    ts = now_ts()

    proximity_state = ProximityState()
    camera_state = CameraState()

    try:
        observation = camera.read_observation(state_id)
        if observation is not None:
            camera_state = CameraState(
                grid=observation.grid,
                description=observation.description,
                target_x=observation.target_x,
            )
    except Exception as exc:  # pragma: no cover
        LOGGER.error("Ошибка чтения камеры: %s", exc)

    try:
        proximity_state = ProximityState(obstacle_cm=proximity.read_distance_cm())
    except Exception as exc:  # pragma: no cover
        LOGGER.warning("Ошибка чтения датчика приближения: %s", exc)

    return RobotState(
        state_id=state_id,
        timestamp=ts,
        sensor=proximity_state,
        camera=camera_state,
    )


def print_stream_instructions(port: int = STREAM_DEFAULT_PORT) -> None:
    """Выводит в консоль инструкцию для подключения к видеопотоку."""
    ip = _get_local_ip()
    print()
    print("  " + "=" * 56)
    print("  ВИДЕО ПОТОК КАМЕРЫ — откройте в браузере:")
    print("  http://{}:{}".format(ip, port))
    print("  (локально: http://127.0.0.1:{})".format(port))
    print("  " + "=" * 56)
    print()


def run_vision_loop(config: VisionConfig, stop_event: Optional[threading.Event] = None) -> None:
    """Основной цикл vision: синхронный capture -> LLM -> ultrasonic -> state write."""
    stop_event = stop_event or threading.Event()
    _clear_capture_images(config.capture_dir)

    frame_buffer: Optional[FrameBuffer] = None
    if config.stream_enabled and cv2 is not None:
        frame_buffer = FrameBuffer()
        run_stream_server(config.stream_port, frame_buffer, stop_event)
        print_stream_instructions(config.stream_port)

    proximity, camera = build_sensors(config, frame_buffer=frame_buffer)
    counter = 0
    LOGGER.info("Vision запущен. state_path=%s mode=sync", STATE_PATH)

    try:
        while not stop_event.is_set():
            counter += 1
            state = _build_state(counter, proximity, camera)
            atomic_write_json(STATE_PATH, state.to_dict())
    finally:
        camera.close()
        LOGGER.info("Vision остановлен")


def parse_args() -> VisionConfig:
    parser = argparse.ArgumentParser(description="Vision module")
    parser.add_argument("--capture-keep-last", type=int, default=VisionConfig.capture_keep_last, help="Сколько последних снимков хранить")
    parser.add_argument("--ollama-base-url", default=VisionConfig.ollama_base_url, help="Ollama URL, e.g. http://192.168.1.100:11434")
    parser.add_argument("--ollama-model", default=VisionConfig.ollama_model, help="Ollama model tag for vision")
    parser.add_argument("--ollama-timeout-s", type=float, default=VisionConfig.ollama_timeout_s, help="Timeout запроса vision модели")
    parser.add_argument("--ollama-temperature", type=float, default=VisionConfig.ollama_temperature, help="Sampling temperature для vision модели")
    parser.add_argument("--ollama-num-predict", type=int, default=VisionConfig.ollama_num_predict, help="Макс. токенов в ответе vision модели")
    parser.add_argument("--stream-port", type=int, default=STREAM_DEFAULT_PORT, help="Порт для видеопотока в браузере")
    parser.add_argument("--no-stream", action="store_true", help="Отключить видеопоток в браузере")
    args = parser.parse_args()
    return VisionConfig(
        capture_keep_last=max(1, int(args.capture_keep_last)),
        ollama_base_url=str(args.ollama_base_url),
        ollama_model=str(args.ollama_model),
        ollama_timeout_s=max(0.1, float(args.ollama_timeout_s)),
        ollama_temperature=max(0.0, float(args.ollama_temperature)),
        ollama_num_predict=max(1, int(args.ollama_num_predict)),
        stream_port=max(1024, int(args.stream_port)),
        stream_enabled=not args.no_stream,
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
