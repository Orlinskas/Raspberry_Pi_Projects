# robot_prome_v1

Легкая модульная архитектура управления роботом через JSON-файлы.

## Что делает каждый модуль

- `vision.py` — генерирует новое состояние робота и пишет `state.json`
- `brain.py` — читает `state.json`, принимает решение, пишет `command.json`
- `controller.py` — исполняет команду из `command.json` на моторах
- `feelings.py` — переносит текущую исполняемую команду в `state.feelings`
- `main.py` — поднимает все потоки и корректно завершает систему
- `shared.py` — общие модели (`RobotState`, `RobotCommand`, `FeelingsState`) и безопасный JSON I/O

## Схема взаимодействия

```mermaid
flowchart LR
    sensors["SensorsCamera"] --> vision["vision.py"]
    vision -->|"write"| state["state.json"]
    state -->|"read new state_id"| brain["brain.py"]
    brain -->|"write"| command["command.json"]
    command -->|"read/execute"| controller["controller.py"]
    command -->|"read"| feelings["feelings.py"]
    feelings -->|"update feelings block"| state
    main["main.py"] --> vision
    main --> brain
    main --> controller
    main --> feelings
```

### Блок-схема основных модулей

```mermaid
flowchart TD
    mainModule["main.py"]
    visionModule["vision.py"]
    brainModule["brain.py"]
    controllerModule["controller.py"]
    feelingsModule["feelings.py"]
    stateFile["state.json"]
    commandFile["command.json"]

    mainModule --> visionModule
    mainModule --> brainModule
    mainModule --> controllerModule
    mainModule --> feelingsModule

    visionModule -->|"пишет"| stateFile
    brainModule -->|"читает"| stateFile
    brainModule -->|"пишет"| commandFile
    controllerModule -->|"читает и исполняет"| commandFile
    feelingsModule -->|"читает"| commandFile
    feelingsModule -->|"обновляет feelings"| stateFile
```

### Шаги цикла

```mermaid
flowchart LR
    step1["1) vision: генерирует новый state_id и пишет state.json"]
    step2["2) brain: читает новый state и создает command.json"]
    step3["3) controller: исполняет command (speed + duration_ms)"]
    step4["4) feelings: переносит текущую команду в state.feelings"]

    step1 --> step2 --> step3 --> step4 --> step1
```

## Формат `state.json`

`state.json` содержит входы сенсоров + блок `feelings`:

- `schema_version`
- `state_id`
- `timestamp`
- `proximity.distance_cm`
- `proximity.valid`
- `camera.obstacle`
- `camera.target_x`
- `camera.confidence`
- `camera.valid`
- `feelings.command_id`
- `feelings.action`
- `feelings.speed`
- `feelings.duration_ms`
- `feelings.reason`
- `feelings.updated_at`

## Формат `command.json`

- `schema_version`
- `command_id`
- `timestamp`
- `based_on_state_id`
- `action` (`FORWARD`, `BACKWARD`, `TURN_LEFT`, `TURN_RIGHT`, `STOP`)
- `params.speed`
- `params.duration_ms`
- `reason`

## Поведение системы

- `vision` работает с интервалом (`--interval`) и создает новый `state_id`
- `brain` не имеет собственного интервала генерации команд:
  - обрабатывает только новый `state_id`
  - если `state_id` не изменился, просто ждет
- `controller` исполняет действие и держит его `params.duration_ms`
- `feelings` фиксирует последнюю выполненную команду в `state.feelings`
- при завершении `main` сбрасывает `state.json` и `command.json` в нулевое состояние

## Логи

- `brain` выводит `STATE used` и `COMMAND generated` в консоль в pretty-print JSON
- остальные модули логируют жизненный цикл и технические события

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

### Controller (автоматический режим)

```bash
python3 controller.py --mode loop --poll 0.05
```

### Feelings

```bash
python3 feelings.py --poll 0.05
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

## Текущее ограничение

`vision` и `feelings` оба пишут в `state.json`, поэтому для реального прод-режима лучше перейти на единый writer или очередь событий.
