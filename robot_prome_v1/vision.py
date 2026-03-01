#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# Author:   Vlad Orlinskas
# Site:     https://prometeriy.com
# Project:  robot_prome_v1 — LLM-driven autonomous robot experiment
# License:  Free for any use
# ---------------------------------------------------------------------------

from __future__ import annotations

import argparse
import json
import logging
import os
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
from typing import Any, Deque, Optional, Protocol, Tuple

from shared import (
    GPIO_LOCK,
    CameraState,
    ProximityState,
    RobotState,
    atomic_write_json,
    get_effective_duration_ms,
    read_json,
)

LOGGER = logging.getLogger("vision")
STATE_PATH = Path(__file__).with_name("protocol") / "state.json"
CAPTURE_DIR = Path(__file__).with_name("captures")
COMMAND_PATH = Path(__file__).with_name("protocol") / "command.json"
VISION_POLL_WAIT_S = 0.1
VISION_EXTRA_DELAY_S = 1.0

ECHO_PIN = 0
TRIG_PIN = 1
ULTRASONIC_TIMEOUT_S = 0.03
ULTRASONIC_MIN_CM = 2.0
ULTRASONIC_MAX_CM = 500.0
ULTRASONIC_INTER_MEASURE_DELAY_S = 0.06
ULTRASONIC_SAMPLES_PER_READ = 5
ULTRASONIC_OUTLIER_RATIO = 0.4

CAMERA_INDEX = 0
CAMERA_WIDTH = 640
CAMERA_HEIGHT = 480
CAMERA_FPS = 30.0
CAMERA_WARMUP_S = 1.0
CAPTURE_KEEP_LAST = 30

try:
    import RPi.GPIO as GPIO
except ImportError:
    GPIO = None

try:
    import cv2
except ImportError:
    cv2 = None

STREAM_DEFAULT_PORT = 8765


class FrameBuffer:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame: Optional[bytes] = None

    def put(self, jpeg_bytes: bytes) -> None:
        with self._lock:
            self._frame = bytes(jpeg_bytes)

    def get(self) -> Optional[bytes]:
        with self._lock:
            return self._frame if self._frame is not None else None


class ProximitySensor(Protocol):
    def read_distance_cm(self) -> float:
        ...


class CameraDetector(Protocol):
    def read_image_path(self, state_id: str) -> Optional[str]:
        ...

    def close(self) -> None:
        ...


class UltrasonicProximitySensor:
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
        if len(samples) < 2:
            return samples
        med = statistics.median(samples)
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
    def read_image_path(self, state_id: str) -> Optional[str]:
        _ = state_id
        return None

    def close(self) -> None:
        pass


class OpenCVCameraDetector:
    def __init__(
        self,
        capture_dir: Path,
        camera_index: int = CAMERA_INDEX,
        width: int = CAMERA_WIDTH,
        height: int = CAMERA_HEIGHT,
        fps: float = CAMERA_FPS,
        keep_last: int = CAPTURE_KEEP_LAST,
        frame_buffer: Optional[FrameBuffer] = None,
    ) -> None:
        self._capture_dir = capture_dir
        self._frame_buffer = frame_buffer
        self._camera_index = camera_index
        self._width = width
        self._height = height
        self._fps = fps
        self._keep_last = max(1, int(keep_last))
        self._cap = None
        self._open_warning_logged = False

    def _ensure_open(self) -> bool:
        if cv2 is None:
            if not self._open_warning_logged:
                LOGGER.warning("OpenCV unavailable, camera disabled")
                self._open_warning_logged = True
            return False

        if self._cap is not None and self._cap.isOpened():
            return True

        self.close()
        cap = cv2.VideoCapture(self._camera_index)
        if not cap.isOpened():
            cap.release()
            if not self._open_warning_logged:
                LOGGER.warning("USB camera open failed index=%s", self._camera_index)
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

    def read_image_path(self, state_id: str) -> Optional[str]:
        if not self._ensure_open():
            return None

        assert self._cap is not None
        ok, frame = self._cap.read()
        if not ok or frame is None:
            LOGGER.warning("USB camera frame read failed")
            return None

        self._capture_dir.mkdir(parents=True, exist_ok=True)
        image_path = self._capture_dir / f"{state_id}.jpg"
        if not cv2.imwrite(str(image_path), frame):
            LOGGER.warning("Frame save failed: %s", image_path)
            return None

        if self._frame_buffer is not None and cv2 is not None:
            _, jpeg = cv2.imencode(".jpg", frame)
            if jpeg is not None:
                self._frame_buffer.put(jpeg.tobytes())

        _prune_capture_images(self._capture_dir, keep_last=self._keep_last)
        return str(image_path.resolve())

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None


def _get_local_ip() -> str:
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
    capture_dir: Path = CAPTURE_DIR
    capture_keep_last: int = CAPTURE_KEEP_LAST
    stream_port: int = STREAM_DEFAULT_PORT
    stream_enabled: bool = True
    command_path: Path = COMMAND_PATH


def build_sensors(
    config: VisionConfig,
    frame_buffer: Optional[FrameBuffer] = None,
) -> Tuple[ProximitySensor, CameraDetector]:
    if cv2 is None:
        LOGGER.error("cv2 not found, using MockCameraDetector")
        camera: CameraDetector = MockCameraDetector()
    else:
        camera = OpenCVCameraDetector(
            capture_dir=config.capture_dir,
            keep_last=config.capture_keep_last,
            frame_buffer=frame_buffer,
        )
    return UltrasonicProximitySensor(), camera


def _clear_capture_images(capture_dir: Path) -> None:
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
        except OSError as exc:
            LOGGER.warning("Delete failed %s: %s", path, exc)
    if deleted:
        LOGGER.info("Cleaned captures: %s files removed", deleted)


def _wait_for_command_duration(
    command_path: Path,
    last_processed_command_id: str,
    stop_event: threading.Event,
) -> Optional[str]:
    while not stop_event.is_set():
        raw = read_json(command_path)
        if not isinstance(raw, dict):
            stop_event.wait(VISION_POLL_WAIT_S)
            continue
        command_id = str(raw.get("command_id", ""))
        if not command_id:
            stop_event.wait(VISION_POLL_WAIT_S)
            continue
        if command_id == last_processed_command_id:
            stop_event.wait(VISION_POLL_WAIT_S)
            continue
        action = str(raw.get("action", "LIGHT_OFF"))
        duration_ms = get_effective_duration_ms(action)
        duration_s = duration_ms / 1000.0 + VISION_EXTRA_DELAY_S
        LOGGER.info("Vision: cmd %s (%s), wait %.2fs", command_id, action, duration_s)
        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline and not stop_event.is_set():
            stop_event.wait(min(VISION_POLL_WAIT_S, max(0, deadline - time.monotonic())))
        return command_id
    return None


def _prune_capture_images(capture_dir: Path, keep_last: int) -> None:
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
        except OSError as exc:
            LOGGER.warning("Delete failed %s: %s", old_path, exc)


def _build_state(state_counter: int, proximity: ProximitySensor, camera: CameraDetector) -> RobotState:
    state_id = f"st_{state_counter:06d}"

    proximity_state = ProximityState()
    camera_state = CameraState()

    try:
        image_path = camera.read_image_path(state_id)
        if image_path is not None:
            camera_state = CameraState(image_path=image_path)
    except Exception as exc:
        LOGGER.error("Camera read error: %s", exc)

    try:
        proximity_state = ProximityState(obstacle_cm=proximity.read_distance_cm())
    except Exception as exc:
        LOGGER.warning("Proximity sensor error: %s", exc)

    return RobotState(
        state_id=state_id,
        sensor=proximity_state,
        camera=camera_state,
    )


def print_stream_instructions(port: int = STREAM_DEFAULT_PORT) -> None:
    ip = _get_local_ip()
    print()
    print("  " + "=" * 56)
    print("  Camera stream — open in browser:")
    print("  http://{}:{}".format(ip, port))
    print("  (local: http://127.0.0.1:{})".format(port))
    print("  " + "=" * 56)
    print()


def run_vision_loop(config: VisionConfig, stop_event: Optional[threading.Event] = None) -> None:
    stop_event = stop_event or threading.Event()
    _clear_capture_images(config.capture_dir)

    frame_buffer: Optional[FrameBuffer] = None
    if config.stream_enabled and cv2 is not None:
        frame_buffer = FrameBuffer()
        run_stream_server(config.stream_port, frame_buffer, stop_event)
        print_stream_instructions(config.stream_port)

    proximity, camera = build_sensors(config, frame_buffer=frame_buffer)
    counter = 0
    LOGGER.info("Vision started state_path=%s", STATE_PATH)

    last_processed_command_id = ""
    try:
        while not stop_event.is_set():
            command_id = _wait_for_command_duration(
                config.command_path,
                last_processed_command_id,
                stop_event,
            )
            if command_id is None:
                break
            last_processed_command_id = command_id
            counter += 1
            state = _build_state(counter, proximity, camera)
            state_payload = state.to_dict()
            atomic_write_json(STATE_PATH, state_payload)
            LOGGER.info("STATE written:\n%s", json.dumps(state_payload, ensure_ascii=False, indent=2, sort_keys=True))
    finally:
        camera.close()
        LOGGER.info("Vision stopped")


def parse_args() -> VisionConfig:
    parser = argparse.ArgumentParser(description="Vision module (capture + ultrasonic, no LLM)")
    parser.add_argument("--capture-keep-last", type=int, default=VisionConfig.capture_keep_last, help="Сколько последних снимков хранить")
    parser.add_argument("--stream-port", type=int, default=STREAM_DEFAULT_PORT, help="Порт для видеопотока в браузере")
    parser.add_argument("--no-stream", action="store_true", help="Отключить видеопоток в браузере")
    parser.add_argument("--command-path", default=str(COMMAND_PATH), help="Path to protocol/command.json")
    args = parser.parse_args()
    return VisionConfig(
        capture_keep_last=max(1, int(args.capture_keep_last)),
        stream_port=max(1024, int(args.stream_port)),
        stream_enabled=not args.no_stream,
        command_path=Path(args.command_path),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    config = parse_args()
    try:
        run_vision_loop(config)
    except KeyboardInterrupt:
        LOGGER.info("Vision stopped by user")


if __name__ == "__main__":
    main()
