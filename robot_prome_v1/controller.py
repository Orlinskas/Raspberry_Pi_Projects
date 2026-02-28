#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Модуль controller: исполняет команды из `protocol/command.json` на моторах."""

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

from shared import (
    ACTION_DURATION_MS,
    ACTION_SPEED,
    GPIO_LOCK,
    RobotCommand,
    read_json,
)

# GPIO-пины моторов (BCM)
IN1, IN2, IN3, IN4 = 20, 21, 19, 26
ENA, ENB = 16, 13
# RGB LED (Yahboom Tank: ColorLED.py)
LED_R, LED_G, LED_B = 22, 27, 24

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
        GPIO.setup(LED_R, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(LED_G, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(LED_B, GPIO.OUT, initial=GPIO.LOW)

        pwm_ena = GPIO.PWM(ENA, 2000)
        pwm_enb = GPIO.PWM(ENB, 2000)
        pwm_ena.start(0)
        pwm_enb.start(0)


def forward(speed: int = 30):
    GPIO.output(IN1, GPIO.HIGH)
    GPIO.output(IN2, GPIO.LOW)
    GPIO.output(IN3, GPIO.HIGH)
    GPIO.output(IN4, GPIO.LOW)
    pwm_ena.ChangeDutyCycle(speed)
    pwm_enb.ChangeDutyCycle(speed)


def backward(speed: int = 30):
    GPIO.output(IN1, GPIO.LOW)
    GPIO.output(IN2, GPIO.HIGH)
    GPIO.output(IN3, GPIO.LOW)
    GPIO.output(IN4, GPIO.HIGH)
    pwm_ena.ChangeDutyCycle(speed)
    pwm_enb.ChangeDutyCycle(speed)


def turn_left(speed: int = 30):
    GPIO.output(IN1, GPIO.LOW)
    GPIO.output(IN2, GPIO.HIGH)
    GPIO.output(IN3, GPIO.HIGH)
    GPIO.output(IN4, GPIO.LOW)
    pwm_ena.ChangeDutyCycle(speed)
    pwm_enb.ChangeDutyCycle(speed)


def turn_right(speed: int = 30):
    GPIO.output(IN1, GPIO.HIGH)
    GPIO.output(IN2, GPIO.LOW)
    GPIO.output(IN3, GPIO.LOW)
    GPIO.output(IN4, GPIO.HIGH)
    pwm_ena.ChangeDutyCycle(speed)
    pwm_enb.ChangeDutyCycle(speed)


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


def light_on():
    """Включает RGB LED (белый: R+G+B)."""
    GPIO.output(LED_R, GPIO.HIGH)
    GPIO.output(LED_G, GPIO.HIGH)
    GPIO.output(LED_B, GPIO.HIGH)


def light_off():
    """Выключает RGB LED."""
    GPIO.output(LED_R, GPIO.LOW)
    GPIO.output(LED_G, GPIO.LOW)
    GPIO.output(LED_B, GPIO.LOW)


def cleanup():
    """Безопасная деинициализация GPIO."""
    global pwm_ena, pwm_enb
    with GPIO_LOCK:
        light_off()
        stop()
        if pwm_ena is not None:
            pwm_ena.stop()
            pwm_ena = None
        if pwm_enb is not None:
            pwm_enb.stop()
            pwm_enb = None
        GPIO.cleanup()


def execute_command(command: RobotCommand) -> None:
    """Маппинг action -> функция движения.
    Все параметры (speed, duration_ms) заданы в shared.ACTION_SPEED и ACTION_DURATION_MS.
    """
    global _ACTION_UNTIL_TS

    action = command.action
    speed = ACTION_SPEED.get(action, 30)
    duration_ms = ACTION_DURATION_MS.get(action, 0)

    if action == "STEP_FORWARD":
        forward(speed=speed)
    elif action == "STEP_BACKWARD":
        backward(speed=speed)
    elif action in ("TURN_LEFT_15", "TURN_LEFT_45"):
        turn_left(speed=speed)
    elif action in ("TURN_RIGHT_15", "TURN_RIGHT_45"):
        turn_right(speed=speed)
    elif action == "LIGHT_ON":
        light_on()
    elif action == "LIGHT_OFF":
        light_off()
    else:
        stop()

    _ACTION_UNTIL_TS = time.time() + (duration_ms / 1000.0) if duration_ms > 0 else 0.0

    LOGGER.debug(
        "Исполнена команда id=%s action=%s reason=%s speed=%s",
        command.command_id,
        action,
        command.reason,
        speed if action in ACTION_SPEED else "—",
    )


def execute_command_dry_run(command: RobotCommand) -> None:
    """Обрабатывает команду без движения моторов (для тестового режима)."""
    global _ACTION_UNTIL_TS

    action = command.action
    duration_ms = ACTION_DURATION_MS.get(action, 0)
    _ACTION_UNTIL_TS = time.time() + (duration_ms / 1000.0) if duration_ms > 0 else 0.0

    LOGGER.debug(
        "DRY команда id=%s action=%s reason=%s",
        command.command_id,
        action,
        command.reason,
    )

def run_controller_loop(
    command_path: Union[Path, str] = Path(__file__).with_name("protocol") / "command.json",
    poll_interval_s: float = 0.05,
    stop_event: Optional[threading.Event] = None,
    enable_motors: bool = True,
) -> None:
    """Режим автомата: читает команду из файла и исполняет ее."""
    stop_event = stop_event or threading.Event()
    command_path = Path(command_path)
    last_command_id = ""

    if enable_motors:
        setup()
        LOGGER.info("Controller запущен. command_path=%s", command_path)
    else:
        LOGGER.info("Controller запущен в DRY режиме. command_path=%s", command_path)

    global _ACTION_UNTIL_TS
    try:
        while not stop_event.is_set():
            raw = read_json(command_path)
            if not isinstance(raw, dict):
                if enable_motors:
                    stop()
                stop_event.wait(poll_interval_s)
                continue

            command = RobotCommand.from_dict(raw)
            if command.command_id != last_command_id:
                if enable_motors:
                    execute_command(command)
                else:
                    execute_command_dry_run(command)
                last_command_id = command.command_id
            elif 0 < _ACTION_UNTIL_TS <= time.time():
                if enable_motors:
                    stop()
                _ACTION_UNTIL_TS = 0.0
            stop_event.wait(poll_interval_s)
    finally:
        if enable_motors:
            cleanup()
        LOGGER.info("Controller остановлен")


def interactive_main():
    """Ручной режим для отладки по клавишам."""
    setup()
    print("Controller started.")
    print("Commands: W - forward, S - backward, A - left, D - right, C - stop, L - light on, O - light off, Q - quit")

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
            elif command == "L":
                light_on()
                print("Light on")
            elif command == "O":
                light_off()
                print("Light off")
            elif command == "Q":
                break
            else:
                print("Unknown command. Use W/S/A/D/C/L/O/Q")
    except KeyboardInterrupt:
        pass
    finally:
        cleanup()
        print("Controller stopped.")


def parse_args():
    parser = argparse.ArgumentParser(description="Robot controller module")
    parser.add_argument("--mode", choices=["interactive", "loop"], default="interactive")
    return parser.parse_args()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    args = parse_args()
    if args.mode == "interactive":
        interactive_main()
    else:
        try:
            run_controller_loop()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
