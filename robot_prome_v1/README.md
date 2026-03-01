# robot_prome_v1

| | |
|---|---|
| **Автор** | Vlad Orlinskas |
| **Сайт** | [prometeriy.com](https://prometeriy.com) |
| **Цель** | Эксперимент: автономный робот на LLM (vision + brain) |
| **Лицензия** | Свободное использование |

Легкая модульная архитектура управления роботом через JSON-файлы.

## Что делает каждый модуль

- `vision.py` — захватывает кадр камеры (OpenCV) и пишет `state.json`
- `brain.py` — читает `state.json`, отправляет изображение + текст в vision-модель (Ollama), принимает решение, пишет `command.json`
- `controller.py` — исполняет команду из `command.json` на моторах
- `main.py` — поднимает все потоки и корректно завершает систему
- `shared.py` — общие модели (`RobotState`, `RobotCommand`) и безопасный JSON I/O

## Схема взаимодействия

```mermaid
flowchart LR
    sensors["SensorsCamera"] --> vision["vision.py"]
    vision -->|"write"| state["state.json"]
    state -->|"read new state_id"| brain["brain.py"]
    brain -->|"write"| command["command.json"]
    command -->|"read/execute"| controller["controller.py"]
    main["main.py"] --> vision
    main --> brain
    main --> controller
```

### Блок-схема основных модулей

```mermaid
flowchart TD
    mainModule["main.py"]
    visionModule["vision.py"]
    brainModule["brain.py"]
    controllerModule["controller.py"]
    stateFile["state.json"]
    commandFile["command.json"]

    mainModule --> visionModule
    mainModule --> brainModule
    mainModule --> controllerModule

    visionModule -->|"пишет"| stateFile
    brainModule -->|"читает"| stateFile
    brainModule -->|"пишет"| commandFile
    controllerModule -->|"читает и исполняет"| commandFile
```

### Шаги цикла

```mermaid
flowchart LR
    step1["1) vision: генерирует новый state_id и пишет state.json"]
    step2["2) brain: читает новый state и создает command.json"]
    step3["3) controller: исполняет command"]

    step1 --> step2 --> step3 --> step1
```

## Формат `state.json`

`state.json` содержит входы сенсоров:

- `state_id`
- `sensor.obstacle_cm` — расстояние до препятствия в см (ultrasonic)
- `camera.image_path` — путь к захваченному изображению (OpenCV) для обработки в brain

## Формат `command.json`

- `command_id`
- `based_on_state_id`
- `action` (`STEP_FORWARD`, `STEP_BACKWARD`, `TURN_LEFT_15`, `TURN_LEFT_45`, `TURN_RIGHT_15`, `TURN_RIGHT_45`, `LIGHT_ON`, `LIGHT_OFF`, `ERROR`, `PLAY`)
- `reason`

## Поведение системы

- `vision` работает с интервалом (`--interval`) и создает новый `state_id`
- `brain` не имеет собственного интервала генерации команд:
  - обрабатывает только новый `state_id`
  - если `state_id` не изменился, просто ждет
- `controller` исполняет действие и держит его заданную длительность (из `shared.ACTION_DURATION_MS`)
- при завершении `main` сбрасывает `state.json` и `command.json` в нулевое состояние

## Логи

- `vision` выводит `STATE written` в консоль в pretty-print JSON
- `brain` выводит `COMMAND generated` в консоль в pretty-print JSON
- остальные модули логируют жизненный цикл и технические события

## Видеопоток камеры

При запуске с камерой (OpenCV) автоматически поднимается MJPEG-сервер. Откройте в браузере URL, который выводится при старте:

```
  ========================================================
  ВИДЕО ПОТОК КАМЕРЫ — откройте в браузере:
  http://192.168.x.x:8765
  (локально: http://127.0.0.1:8765)
  ========================================================
```

- Порт по умолчанию: `8765`. Можно изменить: `python3 main.py --stream-port 9000`
- Отключить поток: `python3 main.py --no-stream`
- Поток использует кадры из основного vision-цикла и не влияет на работу робота

## Быстрый старт

```bash
cd robot_prome_v1
python3 main.py
```

По умолчанию `vision` использует mock-датчики.

## Запуск по отдельности

### Vision

```bash
python3 vision.py --interval 3
```

### Brain

```bash
python3 brain.py
```

Требуется vision-модель (поддержка text + image), например `gemma3`. Пример:

```bash
python3 brain.py \
  --ollama-base-url http://192.168.0.10:11434 \
  --ollama-model gemma3 \
  --ollama-timeout-s 100 \
  --llm-temperature 0.1 \
  --llm-num-predict 256
```

### Controller (автоматический режим)

```bash
python3 controller.py --mode loop --poll 0.05
```

### Controller (ручной режим)

```bash
python3 controller.py --mode interactive
```

## Параметры `main.py`

- `--vision-interval` — интервал генерации `state` (сек)
- `--controller-poll` — частота чтения команд controller (сек)

Пример:

```bash
python3 main.py --vision-interval 3 --controller-poll 0.05
```

## Локальная нейросеть на Raspberry Pi (Ollama)

Все ниже работает полностью локально: модель хранится на Raspberry Pi и запросы из `brain.py` идут только на `localhost`.

### 1) Установка Ollama на Raspberry Pi

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

Проверка, что сервис поднят:

```bash
ollama --version
curl http://192.168.0.10:11434/api/tags
```

### 2) Загрузка vision-модели (text + image)

Рекомендуемый старт для робота:

```bash
ollama pull gemma3
```

Проверить локально скачанные модели:

```bash
ollama list
```

### 3) Проверка генерации ответа от локальной LLM

```bash
ollama run qwen2.5:7b "Return JSON only: {\"action\":\"LIGHT_OFF\",\"reason\":\"healthcheck\"}"
```

### 4) Проверка офлайн-режима

1. После `ollama pull ...` отключить Raspberry Pi от интернета.
2. Повторить `ollama run ...`.
3. Если ответ получен, модель работает полностью локально.

### 5) Интеграция с `brain.py`

`brain.py` отправляет в Ollama изображение с камеры + данные датчиков (vision-модель) и ожидает строго JSON-решение:

- `action`: `STEP_FORWARD | STEP_BACKWARD | TURN_LEFT_15 | TURN_LEFT_45 | TURN_RIGHT_15 | TURN_RIGHT_45 | LIGHT_ON | LIGHT_OFF | ERROR | PLAY`
- `reason`: краткая причина

Параметры движений (speed, duration_ms) заданы в `shared.ACTION_SPEED` и `shared.ACTION_DURATION_MS`.

Если Ollama недоступен, вернул невалидный JSON или неверные поля, `brain.py` записывает fail-safe команду `ERROR` (три быстрых красных мигания LED).

### 6) Диагностика

Проверка HTTP-ответа локального сервиса:

```bash
curl http://192.168.0.10:11434/api/tags
```

Проверка `brain.py` с явным путём протокола:

```bash
python3 brain.py \
  --state-path protocol/state.json \
  --command-path protocol/command.json \
  --ollama-base-url http://192.168.0.10:11434 \
  --ollama-model qwen2.5:7b
```

Если в логах `brain.py` есть `llm_unavailable_fail_safe` или `llm_invalid_response_fail_safe`, проверьте:

- запущен ли `ollama` сервис;
- установлен ли локально тег модели (`ollama list`);
- доступен ли `http://192.168.0.10:11434`.

## Текущие ограничения

`vision` пишет в `state.json`.
