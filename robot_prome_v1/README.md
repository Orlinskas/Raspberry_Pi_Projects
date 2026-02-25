# robot_prome_v1

Модульная архитектура управления роботом на Python:

- `vision` — собирает данные сенсоров и публикует `state.json`
- `brain` — читает `state.json`, принимает решение, публикует `command.json`
- `controller` — читает `command.json` и управляет моторами
- `main` — запускает все модули и следит за их состоянием

## Структура

- `main.py` — orchestrator (потоки `vision`, `brain`, `controller`, health-monitor)
- `vision.py` — цикл чтения сенсоров (по умолчанию mock-режим)
- `brain.py` — логика решений (`observe -> interpret -> decide`)
- `controller.py` — исполнение команд и fail-safe остановка
- `shared.py` — общие модели, JSON I/O, проверка "устаревших" данных
- `state.json` — текущее состояние робота
- `command.json` — текущая команда роботу

## Протокол данных

### state.json (vision -> brain)

Ключевые поля:

- `schema_version`
- `state_id`
- `timestamp`
- `proximity.distance_cm`
- `proximity.valid`
- `camera.obstacle`
- `camera.target_x`
- `camera.confidence`
- `camera.valid`

### command.json (brain -> controller)

Ключевые поля:

- `schema_version`
- `command_id`
- `timestamp`
- `based_on_state_id`
- `action` (`FORWARD`, `BACKWARD`, `TURN_LEFT`, `TURN_RIGHT`, `STOP`)
- `params.speed`
- `params.duration_ms`
- `reason`
- `safety.cancel_if_state_older_ms`

## Быстрый старт

Запуск всего контура:

```bash
cd robot_prome_v1
python3 main.py
```

По умолчанию `vision` работает в mock-режиме, поэтому запуск возможен без железа.

## Запуск модулей отдельно

### Vision

```bash
cd robot_prome_v1
python3 vision.py
```

### Brain

```bash
cd robot_prome_v1
python3 brain.py
```

### Controller (автомат по command.json)

```bash
cd robot_prome_v1
python3 controller.py --mode loop
```

### Controller (ручной режим, клавиши W/S/A/D/C/Q)

```bash
cd robot_prome_v1
python3 controller.py --mode interactive
```

## Параметры запуска

### main.py

- `--state-path` путь до `state.json`
- `--command-path` путь до `command.json`
- `--vision-interval` период `vision` (сек)
- `--brain-interval` период `brain` (сек)
- `--controller-poll` период чтения команд в `controller` (сек)
- `--real` включить реальный режим сенсоров в `vision` (адаптеры еще не подключены)

Пример:

```bash
python3 main.py --vision-interval 0.1 --brain-interval 0.2 --controller-poll 0.05
```

## Безопасность

- `brain` переводит робота в `STOP`, если `state` отсутствует или устарел.
- `controller` переводит робота в `STOP`, если команда устарела.
- `main` завершает систему при критическом падении потока.

## Дальнейшие шаги

1. Подключить реальные адаптеры сенсоров в `vision.py`.
2. Добавить журналирование в файл (rotating logs).
3. Добавить unit-тесты для `BrainEngine` и `shared` моделей.
