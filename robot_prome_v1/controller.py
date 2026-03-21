#!/usr/bin/env python3
# ---------------------------------------------------------------------------
# Author:   Vlad Orlinskas
# Site:     https://prometeriy.com
# Project:  robot_prome_v1 — LLM-driven autonomous robot experiment
# License:  Free for any use
# ---------------------------------------------------------------------------

import argparse
import logging
import threading
import time
from pathlib import Path
from typing import Optional, Union

try:
    import RPi.GPIO as GPIO
except ImportError:
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

from settings import (
    ACTION_DURATION_MS,
    ACTION_SPEED,
    COMMAND_PATH,
    CONTROLLER_ENA,
    CONTROLLER_ENB,
    CONTROLLER_IN1,
    CONTROLLER_IN2,
    CONTROLLER_IN3,
    CONTROLLER_IN4,
    CONTROLLER_LED_B,
    CONTROLLER_LED_G,
    CONTROLLER_LED_R,
    CONTROLLER_POLL_INTERVAL_S,
    CONTROLLER_SERVO_PIN,
    ERROR_BLINK_OFF_S,
    ERROR_BLINK_ON_S,
    GPIO_LOCK,
    PLAY_CYCLES,
    PLAY_PHASE_DURATION_S,
    PLAY_SPEED,
    RobotCommand,
    read_json,
)

pwm_ena = None
pwm_enb = None
pwm_servo = None
LOGGER = logging.getLogger("controller")
_ACTION_UNTIL_TS = 0.0

# Key -> ACTIONS mapping for interactive mode (exact 1:1 with settings.ACTIONS)
INTERACTIVE_KEY_TO_ACTION = {
    "W": "STEP_FORWARD",
    "S": "STEP_BACKWARD",
    "A": "TURN_LEFT_15",
    "Z": "TURN_LEFT_45",
    "D": "TURN_RIGHT_15",
    "X": "TURN_RIGHT_45",
    "L": "LIGHT_ON",
    "O": "LIGHT_OFF",
    "E": "ERROR",
    "P": "PLAY",
    "K": "KILL",
}


def setup():
    global pwm_ena, pwm_enb, pwm_servo
    with GPIO_LOCK:
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)

        GPIO.setup(CONTROLLER_ENA, GPIO.OUT, initial=GPIO.HIGH)
        GPIO.setup(CONTROLLER_ENB, GPIO.OUT, initial=GPIO.HIGH)
        GPIO.setup(CONTROLLER_IN1, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(CONTROLLER_IN2, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(CONTROLLER_IN3, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(CONTROLLER_IN4, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(CONTROLLER_LED_R, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(CONTROLLER_LED_G, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(CONTROLLER_LED_B, GPIO.OUT, initial=GPIO.LOW)
        GPIO.setup(CONTROLLER_SERVO_PIN, GPIO.OUT, initial=GPIO.LOW)

        pwm_ena = GPIO.PWM(CONTROLLER_ENA, 2000)
        pwm_enb = GPIO.PWM(CONTROLLER_ENB, 2000)
        pwm_ena.start(0)
        pwm_enb.start(0)
        pwm_servo = GPIO.PWM(CONTROLLER_SERVO_PIN, 50)
        pwm_servo.start(0)


def forward(speed: int = 30):
    GPIO.output(CONTROLLER_IN1, GPIO.HIGH)
    GPIO.output(CONTROLLER_IN2, GPIO.LOW)
    GPIO.output(CONTROLLER_IN3, GPIO.HIGH)
    GPIO.output(CONTROLLER_IN4, GPIO.LOW)
    pwm_ena.ChangeDutyCycle(speed)
    pwm_enb.ChangeDutyCycle(speed)


def backward(speed: int = 30):
    GPIO.output(CONTROLLER_IN1, GPIO.LOW)
    GPIO.output(CONTROLLER_IN2, GPIO.HIGH)
    GPIO.output(CONTROLLER_IN3, GPIO.LOW)
    GPIO.output(CONTROLLER_IN4, GPIO.HIGH)
    pwm_ena.ChangeDutyCycle(speed)
    pwm_enb.ChangeDutyCycle(speed)


def turn_left(speed: int = 30):
    GPIO.output(CONTROLLER_IN1, GPIO.LOW)
    GPIO.output(CONTROLLER_IN2, GPIO.HIGH)
    GPIO.output(CONTROLLER_IN3, GPIO.HIGH)
    GPIO.output(CONTROLLER_IN4, GPIO.LOW)
    pwm_ena.ChangeDutyCycle(speed)
    pwm_enb.ChangeDutyCycle(speed)


def turn_right(speed: int = 30):
    GPIO.output(CONTROLLER_IN1, GPIO.HIGH)
    GPIO.output(CONTROLLER_IN2, GPIO.LOW)
    GPIO.output(CONTROLLER_IN3, GPIO.LOW)
    GPIO.output(CONTROLLER_IN4, GPIO.HIGH)
    pwm_ena.ChangeDutyCycle(speed)
    pwm_enb.ChangeDutyCycle(speed)


def stop():
    GPIO.output(CONTROLLER_IN1, GPIO.LOW)
    GPIO.output(CONTROLLER_IN2, GPIO.LOW)
    GPIO.output(CONTROLLER_IN3, GPIO.LOW)
    GPIO.output(CONTROLLER_IN4, GPIO.LOW)
    if pwm_ena is not None:
        pwm_ena.ChangeDutyCycle(0)
    if pwm_enb is not None:
        pwm_enb.ChangeDutyCycle(0)


def light_on():
    GPIO.output(CONTROLLER_LED_R, GPIO.HIGH)
    GPIO.output(CONTROLLER_LED_G, GPIO.HIGH)
    GPIO.output(CONTROLLER_LED_B, GPIO.HIGH)


def light_off():
    GPIO.output(CONTROLLER_LED_R, GPIO.LOW)
    GPIO.output(CONTROLLER_LED_G, GPIO.LOW)
    GPIO.output(CONTROLLER_LED_B, GPIO.LOW)


def error_blink():
    for _ in range(3):
        GPIO.output(CONTROLLER_LED_R, GPIO.HIGH)
        GPIO.output(CONTROLLER_LED_G, GPIO.LOW)
        GPIO.output(CONTROLLER_LED_B, GPIO.LOW)
        time.sleep(ERROR_BLINK_ON_S)
        GPIO.output(CONTROLLER_LED_R, GPIO.LOW)
        GPIO.output(CONTROLLER_LED_G, GPIO.LOW)
        GPIO.output(CONTROLLER_LED_B, GPIO.LOW)
        time.sleep(ERROR_BLINK_OFF_S)


_PLAY_COLORS = [
    (GPIO.HIGH, GPIO.LOW, GPIO.LOW),
    (GPIO.LOW, GPIO.HIGH, GPIO.LOW),
    (GPIO.LOW, GPIO.LOW, GPIO.HIGH),
    (GPIO.HIGH, GPIO.HIGH, GPIO.LOW),
    (GPIO.HIGH, GPIO.LOW, GPIO.HIGH),
    (GPIO.LOW, GPIO.HIGH, GPIO.HIGH),
]


def _set_led_color(r: int, g: int, b: int) -> None:
    GPIO.output(CONTROLLER_LED_R, r)
    GPIO.output(CONTROLLER_LED_G, g)
    GPIO.output(CONTROLLER_LED_B, b)


def _servo_set_angle(angle: int) -> None:
    """Set servo angle 0–180 (duty 2.5 + 10*angle/180 as in robot_prome_v1/python)."""
    if pwm_servo is None:
        return
    duty = 2.5 + 10 * angle / 180
    pwm_servo.ChangeDutyCycle(duty)


def play(
    phase_duration_s: float = PLAY_PHASE_DURATION_S,
    speed: int = PLAY_SPEED,
    cycles: int = PLAY_CYCLES,
) -> None:
    kill(phase_duration_s, speed, cycles)
    # for i in range(cycles):
    #     if i % 2 == 0:
    #         turn_left(speed=speed)
    #     else:
    #         turn_right(speed=speed)
    #     _set_led_color(*_PLAY_COLORS[i % len(_PLAY_COLORS)])
    #     time.sleep(phase_duration_s)
    # stop()
    # light_off()


def kill(
    phase_duration_s: float = PLAY_PHASE_DURATION_S,
    speed: int = PLAY_SPEED,
    cycles: int = PLAY_CYCLES,
) -> None:
    """KILL sequence: red blink 1s → servo to 180° 1s → servo to 0° 1s → green blink + PLAY-like turns."""
    # 1. Blink red for 1 second
    t_end = time.time() + 1.0
    while time.time() < t_end:
        _set_led_color(GPIO.LOW, GPIO.LOW, GPIO.HIGH)
        time.sleep(ERROR_BLINK_ON_S)
        light_off()
        time.sleep(ERROR_BLINK_OFF_S)

    # 2. Servo to end (180°) for 1 second
    _servo_set_angle(0)
    time.sleep(1.0)

    # 3. Servo to start (0°) for 1 second
    _servo_set_angle(180)
    time.sleep(1.0)

    # 4. Green blink + PLAY-like movements (turn_left/turn_right)
    for i in range(cycles):
        if i % 2 == 0:
            turn_left(speed=speed)
        else:
            turn_right(speed=speed)
        t_phase_end = time.time() + phase_duration_s
        while time.time() < t_phase_end:
            _set_led_color(GPIO.LOW, GPIO.HIGH, GPIO.LOW)
            time.sleep(ERROR_BLINK_ON_S)
            light_off()
            time.sleep(ERROR_BLINK_OFF_S)

    stop()
    _servo_set_angle(180)
    time.sleep(1)
    if pwm_servo is not None:
        pwm_servo.ChangeDutyCycle(0)
    light_off()


def cleanup():
    global pwm_ena, pwm_enb, pwm_servo
    with GPIO_LOCK:
        light_off()
        stop()
        if pwm_ena is not None:
            pwm_ena.stop()
            pwm_ena = None
        if pwm_enb is not None:
            pwm_enb.stop()
            pwm_enb = None
        if pwm_servo is not None:
            pwm_servo.stop()
            pwm_servo = None
        GPIO.cleanup()


def execute_command(command: RobotCommand) -> None:
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
    elif action == "ERROR":
        stop()
        error_blink()
        light_off()
    elif action == "PLAY":
        play(
            phase_duration_s=PLAY_PHASE_DURATION_S,
            speed=speed,
            cycles=PLAY_CYCLES,
        )
    elif action == "KILL":
        play(
            phase_duration_s=PLAY_PHASE_DURATION_S,
            speed=speed,
            cycles=PLAY_CYCLES,
        )
    else:
        stop()

    _ACTION_UNTIL_TS = time.time() + (duration_ms / 1000.0) if duration_ms > 0 else 0.0

    LOGGER.debug("Executed id=%s action=%s reason=%s speed=%s",
        command.command_id, action, command.reason,
        speed if action in ACTION_SPEED else "-")


def execute_command_dry_run(command: RobotCommand) -> None:
    global _ACTION_UNTIL_TS

    action = command.action
    duration_ms = ACTION_DURATION_MS.get(action, 0)
    _ACTION_UNTIL_TS = time.time() + (duration_ms / 1000.0) if duration_ms > 0 else 0.0

    LOGGER.debug("DRY id=%s action=%s reason=%s",
        command.command_id, action, command.reason)

def run_controller_loop(
    command_path: Union[Path, str] = COMMAND_PATH,
    poll_interval_s: float = CONTROLLER_POLL_INTERVAL_S,
    stop_event: Optional[threading.Event] = None,
    enable_motors: bool = True,
) -> None:
    stop_event = stop_event or threading.Event()
    command_path = Path(command_path)
    last_command_id = ""

    if enable_motors:
        setup()
        LOGGER.info("Controller started command_path=%s", command_path)
    else:
        LOGGER.info("Controller started DRY command_path=%s", command_path)

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
        LOGGER.info("Controller stopped")


_interactive_command_counter = 0


def _duration_stop_thread(stop_event: threading.Event) -> None:
    """Background thread: stop motors when ACTION duration expires."""
    global _ACTION_UNTIL_TS
    while not stop_event.is_set():
        if 0 < _ACTION_UNTIL_TS <= time.time():
            stop()
            _ACTION_UNTIL_TS = 0.0
        stop_event.wait(CONTROLLER_POLL_INTERVAL_S)


def interactive_main():
    global _ACTION_UNTIL_TS, _interactive_command_counter
    setup()
    stop_event = threading.Event()
    duration_thread = threading.Thread(
        target=_duration_stop_thread,
        args=(stop_event,),
        name="interactive-duration",
        daemon=True,
    )
    duration_thread.start()

    print("Controller started (interactive, keys map to ACTIONS).")
    print("W STEP_FORWARD | S STEP_BACKWARD")
    print("A TURN_LEFT_15 | Z TURN_LEFT_45 | D TURN_RIGHT_15 | X TURN_RIGHT_45")
    print("L LIGHT_ON | O LIGHT_OFF | E ERROR | P PLAY | K KILL | C stop | Q quit")

    try:
        while True:
            raw = input("Key: ").strip().upper()
            if not raw:
                continue
            key = raw[0]

            if key == "Q":
                break
            if key in ("C", "С"):  # C + Cyrillic С
                stop()
                print("Stop")
                continue

            action = INTERACTIVE_KEY_TO_ACTION.get(key)
            if action is None:
                print(f"Unknown. Keys: {', '.join(sorted(INTERACTIVE_KEY_TO_ACTION))} | C stop | Q quit")
                continue

            _interactive_command_counter += 1
            cmd = RobotCommand(
                command_id=f"int_{_interactive_command_counter:06d}",
                based_on_state_id="interactive",
                action=action,
                reason="manual",
            )
            execute_command(cmd)
            print(action)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        duration_thread.join(timeout=1.0)
        cleanup()
        print("Controller stopped.")


def parse_args():
    parser = argparse.ArgumentParser(description="Robot controller module")
    parser.add_argument("--mode", choices=["interactive", "loop"], default="interactive",
        help="interactive: keyboard control, loop: read command.json")
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
