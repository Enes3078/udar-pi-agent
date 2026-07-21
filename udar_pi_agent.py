#!/usr/bin/env python3
"""
UDAR CRM Raspberry Pi GPIO agent.

Default wiring:
- Physical pin 11 = GPIO17/BCM17 signal input. The machine/button must provide 3.3V pulses here.
- Physical pin 9 = GND/Terra. Machine ground and Raspberry Pi ground must be common.

Never feed 5V, 12V, 24V, or noisy industrial voltage directly into a Raspberry Pi GPIO pin.
Use an optocoupler, relay interface, or level shifter when the machine signal is not clean 3.3V logic.
"""
from __future__ import annotations

import json
import os
import queue
import signal
import socket
import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError as exc:  # pragma: no cover
    raise SystemExit("python3-requests is required. Run: sudo apt install python3-requests") from exc

try:
    from gpiozero import Button, DigitalInputDevice
except ImportError as exc:  # pragma: no cover
    raise SystemExit("python3-gpiozero is required. Run: sudo apt install python3-gpiozero") from exc


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def bool_env(name: str, default: str = "false") -> bool:
    return env(name, default).lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    crm_url: str
    device_token: str
    gpio_bcm: int = 17
    bounce_time: float = 0.05
    pull_up: bool = False
    timeout: float = 5.0
    queue_db: Path = Path("/var/lib/udar-pi-agent/machine_events.sqlite3")
    measurement_mode: str = "pulse"
    duration_unit: str = "seconds"
    min_duration_seconds: float = 0.2
    daily_reset: bool = True
    line_id: str = ""
    station_code: str = ""
    operator_id: str = ""
    note: str = "GPIO event"

    @property
    def endpoint(self) -> str:
        return f"{self.crm_url.rstrip('/')}/api/production/pi/events/"


def load_config() -> Config:
    crm_url = env("UDAR_CRM_URL")
    token = env("UDAR_DEVICE_TOKEN")
    if not crm_url or not token:
        raise SystemExit("UDAR_CRM_URL and UDAR_DEVICE_TOKEN are required in /etc/udar-pi-agent.env")
    mode = env("UDAR_MEASUREMENT_MODE", "pulse").lower()
    if mode not in {"pulse", "duration"}:
        raise SystemExit("UDAR_MEASUREMENT_MODE must be pulse or duration")
    unit = env("UDAR_DURATION_UNIT", "seconds").lower()
    if unit not in {"seconds", "minutes"}:
        raise SystemExit("UDAR_DURATION_UNIT must be seconds or minutes")
    return Config(
        crm_url=crm_url,
        device_token=token,
        gpio_bcm=int(env("UDAR_GPIO_BCM", "17")),
        bounce_time=float(env("UDAR_BOUNCE_SECONDS", "0.05")),
        pull_up=bool_env("UDAR_PULL_UP"),
        timeout=float(env("UDAR_HTTP_TIMEOUT", "5")),
        queue_db=Path(env("UDAR_QUEUE_DB", "/var/lib/udar-pi-agent/machine_events.sqlite3")),
        measurement_mode=mode,
        duration_unit=unit,
        min_duration_seconds=float(env("UDAR_MIN_DURATION_SECONDS", "0.2")),
        daily_reset=bool_env("UDAR_DAILY_RESET", "true"),
        line_id=env("UDAR_LINE_ID"),
        station_code=env("UDAR_STATION_CODE"),
        operator_id=env("UDAR_OPERATOR_ID"),
        note=env("UDAR_NOTE", "GPIO event"),
    )


GPIO_TO_PHYSICAL_PIN = {
    2: 3,
    3: 5,
    4: 7,
    17: 11,
    27: 13,
    22: 15,
    10: 19,
    9: 21,
    11: 23,
    0: 27,
    5: 29,
    6: 31,
    13: 33,
    19: 35,
    26: 37,
    14: 8,
    15: 10,
    18: 12,
    23: 16,
    24: 18,
    25: 22,
    8: 24,
    7: 26,
    1: 28,
    12: 32,
    16: 36,
    20: 38,
    21: 40,
}


class PersistentQueue:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT UNIQUE NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS event_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    response_code INTEGER,
                    response_body TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    sent_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=30)

    def get_state(self, key: str, default: str = "") -> str:
        with self.lock, self._connect() as conn:
            row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else default

    def set_state(self, key: str, value: str) -> None:
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def next_daily_values(self, delta: float, *, reset: bool) -> tuple[int, float, str]:
        today = datetime.now().date().isoformat()
        stored_day = self.get_state("counter_day", today)
        if reset and stored_day != today:
            sequence = 0
            total = 0.0
            stored_day = today
        else:
            sequence = int(float(self.get_state("daily_sequence", "0") or 0))
            total = float(self.get_state("daily_total", "0") or 0)
        sequence += 1
        total += float(delta)
        self.set_state("counter_day", stored_day)
        self.set_state("daily_sequence", str(sequence))
        self.set_state("daily_total", str(total))
        return sequence, total, stored_day

    def put(self, payload: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        body = json.dumps(payload, ensure_ascii=False)
        with self.lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO events (idempotency_key, payload, created_at) VALUES (?, ?, ?)",
                (payload["idempotency_key"], body, now),
            )
            conn.execute(
                "INSERT INTO event_log (idempotency_key, payload, status, created_at) VALUES (?, ?, ?, ?)",
                (payload["idempotency_key"], body, "queued", now),
            )

    def peek(self) -> tuple[int, dict[str, Any]] | None:
        with self.lock, self._connect() as conn:
            row = conn.execute("SELECT id, payload FROM events ORDER BY id LIMIT 1").fetchone()
        if not row:
            return None
        return int(row[0]), json.loads(row[1])

    def mark_sent(self, row_id: int, payload: dict[str, Any], status_code: int, response_body: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.lock, self._connect() as conn:
            conn.execute("DELETE FROM events WHERE id = ?", (row_id,))
            conn.execute(
                """
                INSERT INTO event_log (idempotency_key, payload, status, response_code, response_body, created_at, sent_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["idempotency_key"],
                    json.dumps(payload, ensure_ascii=False),
                    "sent",
                    status_code,
                    response_body[:1000],
                    payload.get("timestamp", now),
                    now,
                ),
            )

    def fail(self, row_id: int, error: str) -> None:
        with self.lock, self._connect() as conn:
            conn.execute("UPDATE events SET attempts = attempts + 1, last_error = ? WHERE id = ?", (error[:500], row_id))


def build_payload(config: Config, *, sequence: int, delta: float, total: float, measured_at: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "token": config.device_token,
        "event_type": "quantity",
        "quantity_delta": round(delta, 2),
        "counter_value": round(total, 2),
        "counter": {
            "total": round(total, 2),
            "delta": round(delta, 2),
            "daily_sequence": sequence,
            "mode": config.measurement_mode,
            "unit": "count" if config.measurement_mode == "pulse" else config.duration_unit,
        },
        "gpio": {
            "bcm": config.gpio_bcm,
            "physical_pin": GPIO_TO_PHYSICAL_PIN.get(config.gpio_bcm),
            "gnd_physical_pin": 9,
        },
        "timestamp": measured_at,
        "host": socket.gethostname(),
        "note": config.note,
        "idempotency_key": f"{config.device_token[:8]}-{socket.gethostname()}-{datetime.now().date().isoformat()}-{sequence}-{uuid.uuid4().hex[:12]}",
    }
    if extra:
        payload.update(extra)
    if config.line_id:
        payload["line_id"] = int(config.line_id)
        payload["line"] = int(config.line_id)
    if config.station_code:
        payload["station_code"] = config.station_code
    if config.operator_id:
        payload["operator_id"] = config.operator_id
    return payload


def sender_loop(config: Config, events: PersistentQueue, stop: threading.Event) -> None:
    session = requests.Session()
    while not stop.is_set():
        item = events.peek()
        if not item:
            stop.wait(0.5)
            continue
        row_id, payload = item
        try:
            response = session.post(config.endpoint, json=payload, timeout=config.timeout)
            if 200 <= response.status_code < 300:
                events.mark_sent(row_id, payload, response.status_code, response.text)
                print(f"sent {payload['idempotency_key']} total={payload['counter']['total']}", flush=True)
            else:
                error = f"HTTP {response.status_code}: {response.text[:300]}"
                events.fail(row_id, error)
                print(error, file=sys.stderr, flush=True)
                stop.wait(2.0)
        except requests.RequestException as exc:
            events.fail(row_id, str(exc))
            print(f"send failed: {exc}", file=sys.stderr, flush=True)
            stop.wait(2.0)


def enqueue_measurement(config: Config, events: PersistentQueue, *, delta: float, extra: dict[str, Any] | None = None) -> None:
    sequence, total, day = events.next_daily_values(delta, reset=config.daily_reset)
    payload = build_payload(
        config,
        sequence=sequence,
        delta=delta,
        total=total,
        measured_at=datetime.now(timezone.utc).isoformat(),
        extra={"counter_day": day, **(extra or {})},
    )
    events.put(payload)
    print(f"{config.measurement_mode} delta={delta:.4f} total={total:.4f} seq={sequence}", flush=True)


def run_pulse_mode(config: Config, events: PersistentQueue, stop: threading.Event) -> None:
    pulse_queue: queue.Queue[None] = queue.Queue()
    button = Button(config.gpio_bcm, pull_up=config.pull_up, bounce_time=config.bounce_time)
    button.when_pressed = lambda: pulse_queue.put(None)
    while not stop.is_set():
        try:
            pulse_queue.get(timeout=0.5)
        except queue.Empty:
            continue
        enqueue_measurement(config, events, delta=1.0)


def run_duration_mode(config: Config, events: PersistentQueue, stop: threading.Event) -> None:
    pin = DigitalInputDevice(config.gpio_bcm, pull_up=config.pull_up)
    started_monotonic: float | None = None
    started_at: str | None = None
    last_value = bool(pin.value)
    if last_value:
        started_monotonic = time.monotonic()
        started_at = datetime.now(timezone.utc).isoformat()
        print("duration started", flush=True)
    while not stop.is_set():
        value = bool(pin.value)
        if value == last_value:
            stop.wait(0.05)
            continue
        last_value = value
        if value and started_monotonic is None:
            started_monotonic = time.monotonic()
            started_at = datetime.now(timezone.utc).isoformat()
            print("duration started", flush=True)
        elif not value and started_monotonic is not None:
            now = time.monotonic()
            elapsed = max(0.0, now - started_monotonic)
            ended_at = datetime.now(timezone.utc).isoformat()
            started_monotonic = None
            if elapsed < config.min_duration_seconds:
                print(f"duration ignored elapsed={elapsed:.3f}s", flush=True)
                continue
            delta = elapsed if config.duration_unit == "seconds" else elapsed / 60.0
            enqueue_measurement(
                config,
                events,
                delta=delta,
                extra={
                    "duration": {
                        "started_at": started_at,
                        "ended_at": ended_at,
                        "seconds": round(elapsed, 2),
                        "is_final_chunk": True,
                    }
                },
            )
            print(f"duration stopped total_elapsed={elapsed:.3f}s", flush=True)


def main() -> int:
    config = load_config()
    events = PersistentQueue(config.queue_db)
    stop = threading.Event()

    def handle_signal(signum, frame):  # noqa: ARG001
        stop.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    sender = threading.Thread(target=sender_loop, args=(config, events, stop), daemon=True)
    sender.start()

    print(
        f"UDAR Pi agent started. mode={config.measurement_mode} BCM GPIO{config.gpio_bcm} signal, physical pin 9 GND/Terra. Endpoint={config.endpoint}",
        flush=True,
    )
    if config.measurement_mode == "duration":
        run_duration_mode(config, events, stop)
    else:
        run_pulse_mode(config, events, stop)

    sender.join(timeout=3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
