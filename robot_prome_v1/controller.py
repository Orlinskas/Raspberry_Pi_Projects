#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Модуль controller: исполняет команды из `command.json` на моторах."""

import argparse
import logging
import threading
import time
from pathlib import Path
from typing import Optional, Union

try:
    import RPi.GPIO as GPIO
except ImportError:  # pragma: no cover
    class _MockPWM:
        def __init__(self, pin, freq):
            self.pin = pin
            self.freq = freq

        def start(self, duty_cycle):
            return None

        def ChangeDutyCycle(self, duty_cycle):
            return None

        def stop(self):
            return None

    class _MockGPIO:
        BCM = "BCM"
        OUT = "OUT"
        HIGH = 1
        LOW = 0

        @staticmethod
        def setmode(mode):
            return None

        @staticmethod
        def setwarnings(flag):
            return None

        @staticmethod
        def setup(pin, mode, initial=0):
            return None

        @staticmethod
        def output(pin, value):
            return None

        @staticmethod
        def PWM(pin, freq):
            return _MockPWM(pin, freq)

        @staticmethod
        def cleanup():
            return None

    GPIO = _MockGPIO()

from shared import GPIO_LOCK, RobotCommand, read_json

# GPIO-пины моторов (BCM)
IN1, IN2, IN3, IN4 = 20, 21, 19, 26
ENA, ENB = 16, 13

# Базовые скорости ШИМ
SPEED = 40
TURN_SPEED = 60

pwm_ena = None
pwm_enb = None
LOGGER = logging.getLogger("controller")
_ACTION_UNTIL_TS = 0.0


def setup():
    """Инициализация GPIO и PWM."""
    global pwm_ena, pwm_enb
    with GPIO_LOCK:
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)

        GPIO.setup(ENA, GPIO.OUT, initial=GPIO.HIGH)
        GPIO.setup(ENB, GPIO.OUT, initial=GPIO.HIGH)
        GPIO.setup(IN1, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(IN2, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(IN3, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(IN4, GPIO.OUT, initial=GPIO.LOW)

        pwm_ena = GPIO.PWM(ENA, 2000)
        pwm_enb = GPIO.PWM(ENB, 2000)
        pwm_ena.start(0)
        pwm_enb.start(0)


def forward():
    GPIO.output(IN1, GPIO.HIGH)
    GPIO.output(IN2, GPIO.LOW)
    GPIO.output(IN3, GPIO.HIGH)
    GPIO.output(IN4, GPIO.LOW)
    pwm_ena.ChangeDutyCycle(SPEED)
    pwm_enb.ChangeDutyCycle(SPEED)


def backward():
    GPIO.output(IN1, GPIO.LOW)
    GPIO.output(IN2, GPIO.HIGH)
    GPIO.output(IN3, GPIO.LOW)
    GPIO.output(IN4, GPIO.HIGH)
    pwm_ena.ChangeDutyCycle(SPEED)
    pwm_enb.ChangeDutyCycle(SPEED)


def turn_left():
    GPIO.output(IN1, GPIO.LOW)
    GPIO.output(IN2, GPIO.HIGH)
    GPIO.output(IN3, GPIO.HIGH)
    GPIO.output(IN4, GPIO.LOW)
    pwm_ena.ChangeDutyCycle(TURN_SPEED)
    pwm_enb.ChangeDutyCycle(TURN_SPEED)


def turn_right():
    GPIO.output(IN1, GPIO.HIGH)
    GPIO.output(IN2, GPIO.LOW)
    GPIO.output(IN3, GPIO.LOW)
    GPIO.output(IN4, GPIO.HIGH)
    pwm_ena.ChangeDutyCycle(TURN_SPEED)
    pwm_enb.ChangeDutyCycle(TURN_SPEED)


def stop():
    """Полная остановка моторов."""
    GPIO.output(IN1, GPIO.LOW)
    GPIO.output(IN2, GPIO.LOW)
    GPIO.output(IN3, GPIO.LOW)
    GPIO.output(IN4, GPIO.LOW)
    if pwm_ena is not None:
        pwm_ena.ChangeDutyCycle(0)
    if pwm_enb is not None:
        pwm_enb.ChangeDutyCycle(0)


def cleanup():
    """Безопасная деинициализация GPIO."""
    global pwm_ena, pwm_enb
    with GPIO_LOCK:
        stop()
        if pwm_ena is not None:
            pwm_ena.stop()
            pwm_ena = None
        if pwm_enb is not None:
            pwm_enb.stop()
            pwm_enb = None
        GPIO.cleanup()


def _clamp_speed(value, fallback):
    try:
        speed = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(0, min(100, speed))


def execute_command(command: RobotCommand) -> None:
    """Маппинг action -> функция движения."""
    global SPEED, TURN_SPEED, _ACTION_UNTIL_TS

    action = command.action
    speed = _clamp_speed(command.params.speed, SPEED)

    if action in ("FORWARD", "BACKWARD"):
        SPEED = speed
    elif action in ("TURN_LEFT", "TURN_RIGHT"):
        TURN_SPEED = speed

    if action == "FORWARD":
        forward()
    elif action == "BACKWARD":
        backward()
    elif action == "TURN_LEFT":
        turn_left()
    elif action == "TURN_RIGHT":
        turn_right()
    else:
        stop()

    duration_ms = max(0, int(command.params.duration_ms))
    _ACTION_UNTIL_TS = time.time() + (duration_ms / 1000.0) if duration_ms > 0 else 0.0

    LOGGER.debug(
        "Исполнена команда id=%s action=%s reason=%s speed=%s",
        command.command_id,
        action,
        command.reason,
        speed,
    )


def run_controller_loop(
    command_path: Union[Path, str] = Path(__file__).with_name("command.json"),
    poll_interval_s: float = 0.05,
    stop_event: Optional[threading.Event] = None,
) -> None:
    """Режим автомата: читает команду из файла и исполняет ее."""
    stop_event = stop_event or threading.Event()
    last_command_id = ""
    setup()
    LOGGER.info("Controller запущен. command_path=%s", command_path)

    try:
        while not stop_event.is_set():
            raw = read_json(command_path)
            if not isinstance(raw, dict):
                stop()
                stop_event.wait(poll_interval_s)
                continue

            command = RobotCommand.from_dict(raw)
            if command.command_id != last_command_id:
                execute_command(command)
                last_command_id = command.command_id
            elif _ACTION_UNTIL_TS > 0 and time.time() >= _ACTION_UNTIL_TS:
                stop()
            stop_event.wait(poll_interval_s)
    finally:
        cleanup()
        LOGGER.info("Controller остановлен")


def interactive_main():
    """Ручной режим для отладки по клавишам."""
    setup()
    print("Controller started.")
    print("Commands: W - forward, S - backward, A - left, D - right, C - stop, Q - quit")

    try:
        while True:
            command = input("Enter command: ").strip().upper()

            if command == "W":
                forward()
                print("Moving forward")
            elif command == "S":
                backward()
                print("Moving backward")
            elif command == "A":
                turn_left()
                print("Turning left")
            elif command == "D":
                turn_right()
                print("Turning right")
            elif command in ("C", "С"):
                stop()
                print("Stop")
            elif command == "Q":
                break
            else:
                print("Unknown command. Use W/S/A/D/C/Q")
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()
        print("Controller stopped.")


def parse_args():
    parser = argparse.ArgumentParser(description="Robot controller module")
    parser.add_argument("--mode", choices=["interactive", "loop"], default="interactive")
    parser.add_argument("--command-path", default=str(Path(__file__).with_name("command.json")))
    parser.add_argument("--poll", type=float, default=0.05)
    return parser.parse_args()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    args = parse_args()
    if args.mode == "interactive":
        interactive_main()
    else:
        try:
            run_controller_loop(command_path=args.command_path, poll_interval_s=max(0.02, float(args.poll)))
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
