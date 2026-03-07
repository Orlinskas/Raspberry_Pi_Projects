# Buying guide for robot_prome_v1

[Русский](BUYING_GUIDE_RU.md)

A short checklist of what to buy to build the robot for this project.

---

## What the project needs (from the code)

| Component | Role in the project |
|-----------|---------------------|
| **Raspberry Pi** | Brain: Python, OpenCV, Ollama (or cloud LLM), GPIO |
| **L298N motor driver** | Two PWM channels (IN1–IN4, ENA, ENB) — forward/back and turns |
| **2 DC motors** | Wheels: forward/back steps, in-place turns |
| **HC-SR04 ultrasonic sensor** | `obstacle_cm` — distance to obstacle ahead |
| **Camera** | OpenCV, 640×480: frame for LLM and browser video stream |
| **RGB LED** | LIGHT_ON / LIGHT_OFF, status (optional — can be disabled in code) |
| **Power** | 6–12 V for motors + 5 V for Pi (battery or PSU) |

Pinout for this setup is in `settings.py`: L298N (IN1–IN4, ENA, ENB), HC-SR04 (TRIG/ECHO), RGB (3 pins), and optionally a servo on one pin.

---

## Buying options

### 1. Ready-made “Raspberry Pi robot car” kit (fastest start)

Look for kits such as:

- **“Raspberry Pi Robot Car Kit”** / **“2WD Smart Car Kit”** with Raspberry Pi  
- They usually include: 2WD chassis, L298N (or similar), 2 DC motors, HC-SR04, sometimes mounting for Pi and camera.

**Check the description for:**

- **Raspberry Pi** compatibility (GPIO, 5 V).
- **L298N** (or compatible driver with IN1–IN4 and ENA, ENB).
- **Ultrasonic sensor** (HC-SR04 or compatible).
- Space for **camera** (USB webcam or CSI) and optionally a battery pack.

If the kit uses a different driver (e.g. TB6612), you will need to change the pin constants in `settings.py` to match your wiring.

---

### 2. Buying parts separately

| Item | Examples | Note |
|------|----------|------|
| Raspberry Pi 4 (2–4 GB) or Pi 5 | Official or partner boards | Pi 3 is fine if using cloud LLM. |
| L298N driver | Dual-channel module | Often sold with heatsink. |
| 2WD chassis | Two wheels + motors, platform | Size to fit Pi and battery. |
| HC-SR04 | Ultrasonic sensor | TRIG + ECHO, 5 V. |
| USB camera | Any Linux/OpenCV-compatible | Or Raspberry Pi Camera (CSI). |
| RGB LED (optional) | 1× common cathode/anode | Or leave unconnected and skip LIGHT_* in code. |
| Battery | 2S LiPo 7.4 V or 6×AA holder | Plus separate 5 V for Pi (step-down or PowerBank). |
| Wires, breadboard | Dupont, breadboard | For GPIO connections. |

Project pins (BCM): motors IN1=20, IN2=21, IN3=19, IN4=26, ENA=16, ENB=13; sensor ECHO=0, TRIG=1; RGB: 22, 27, 24. For different wiring, change the constants in `settings.py`.

---

### 3. Quick tips

- Ready-made kit: ensure it has **L298N** and **HC-SR04** and is advertised for **Raspberry Pi** — less wiring hassle.
- Camera: any USB camera supported on Linux or official Pi Camera; code uses device index 0 and 640×480.
- The robot’s brain is a cloud LLM via Ollama; a powerful Pi is not required for logic, but needed to run Python, camera, and GPIO.
- All pins are configured in a single file, `settings.py` — adjust only there for your hardware.

---

## Minimal setup for “dry” run (no hardware)

To test logic and camera on your own machine only:

- Python 3.8+, OpenCV, Ollama (or cloud endpoint).
- Run: `python main.py --mode dry` — motors and sensor are not used.

For the full robot you need a Raspberry Pi and the components listed above.
