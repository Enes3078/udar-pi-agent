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
    from gpiozero import DigitalInputDevice
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
    pulse_edge: str = "rising"
    poll_interval_seconds: float = 0.002
    pulse_min_active_seconds: float = 0.02
    pulse_rearm_seconds: float = 0.20
    pulse_max_active_seconds: float = 10.0
    pulse_min_interval_seconds: float = 0.20
    duration_unit: str = "seconds"
    min_duration_seconds: float = 0.2
    duration_start_stable_seconds: float = 0.20
    duration_stop_stable_seconds: float = 0.20
    duration_active_level: str = "high"
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
    pulse_edge = env("UDAR_PULSE_EDGE", "rising").lower()
    if pulse_edge not in {"rising", "falling", "both"}:
        raise SystemExit("UDAR_PULSE_EDGE must be rising, falling, or both")
    unit = env("UDAR_DURATION_UNIT", "seconds").lower()
    if unit not in {"seconds", "minutes"}:
        raise SystemExit("UDAR_DURATION_UNIT must be seconds or minutes")
    duration_active_level = env("UDAR_DURATION_ACTIVE_LEVEL", "high").lower()
    if duration_active_level not in {"high", "low"}:
        raise SystemExit("UDAR_DURATION_ACTIVE_LEVEL must be high or low")
    return Config(
        crm_url=crm_url,
        device_token=token,
        gpio_bcm=int(env("UDAR_GPIO_BCM", "17")),
        bounce_time=float(env("UDAR_BOUNCE_SECONDS", "0.05")),
        pull_up=bool_env("UDAR_PULL_UP"),
        timeout=float(env("UDAR_HTTP_TIMEOUT", "5")),
        queue_db=Path(env("UDAR_QUEUE_DB", "/var/lib/udar-pi-agent/machine_events.sqlite3")),
        measurement_mode=mode,
        pulse_edge=pulse_edge,
        poll_interval_seconds=float(env("UDAR_POLL_INTERVAL_SECONDS", "0.002")),
        pulse_min_active_seconds=float(env("UDAR_PULSE_MIN_ACTIVE_SECONDS", "0.02")),
        pulse_rearm_seconds=float(env("UDAR_PULSE_REARM_SECONDS", "0.20")),
        pulse_max_active_seconds=float(env("UDAR_PULSE_MAX_ACTIVE_SECONDS", "10.0")),
        pulse_min_interval_seconds=float(env("UDAR_PULSE_MIN_INTERVAL_SECONDS", "0.20")),
        duration_unit=unit,
        min_duration_seconds=float(env("UDAR_MIN_DURATION_SECONDS", "0.2")),
        duration_start_stable_seconds=float(env("UDAR_DURATION_START_STABLE_SECONDS", "0.20")),
        duration_stop_stable_seconds=float(env("UDAR_DURATION_STOP_STABLE_SECONDS", "0.20")),
        duration_active_level=duration_active_level,
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


class PulseCycleDetector:
    """Accept only a complete, stable inactive -> active -> inactive cycle."""

    def __init__(
        self,
        *,
        active_level: bool,
        min_active_seconds: float,
        rearm_seconds: float,
        max_active_seconds: float,
        min_interval_seconds: float,
    ):
        self.active_level = active_level
        self.min_active_seconds = max(0.0, min_active_seconds)
        self.rearm_seconds = max(0.0, rearm_seconds)
        self.max_active_seconds = max(0.0, max_active_seconds)
        self.min_interval_seconds = max(0.0, min_interval_seconds)
        self.stable_value: bool | None = None
        self.candidate_value: bool | None = None
        self.candidate_since = 0.0
        self.inactive_since: float | None = None
        self.active_started_at: float | None = None
        self.last_emitted_at = float("-inf")

    def feed(self, value: bool, now: float) -> dict[str, Any] | None:
        value = bool(value)
        if self.stable_value is None:
            self.stable_value = value
            self.candidate_value = value
            self.candidate_since = now
            if value != self.active_level:
                self.inactive_since = now
            return None

        if value != self.candidate_value:
            self.candidate_value = value
            self.candidate_since = now
            return None

        if value == self.stable_value:
            return None

        required_stable = self.min_active_seconds if value == self.active_level else self.rearm_seconds
        if now - self.candidate_since < required_stable:
            return None

        previous = self.stable_value
        self.stable_value = value

        if value == self.active_level:
            inactive_for = (
                self.candidate_since - self.inactive_since
                if self.inactive_since is not None
                else 0.0
            )
            if previous != self.active_level and inactive_for >= self.rearm_seconds:
                self.active_started_at = self.candidate_since
            else:
                self.active_started_at = None
            return None

        self.inactive_since = self.candidate_since
        if previous != self.active_level or self.active_started_at is None:
            return None

        active_seconds = max(0.0, self.candidate_since - self.active_started_at)
        self.active_started_at = None
        if active_seconds < self.min_active_seconds:
            return None
        if self.max_active_seconds > 0 and active_seconds > self.max_active_seconds:
            return {
                "accepted": False,
                "reason": "active_too_long",
                "active_seconds": active_seconds,
            }
        if self.candidate_since - self.last_emitted_at < self.min_interval_seconds:
            return {
                "accepted": False,
                "reason": "rate_limited",
                "active_seconds": active_seconds,
            }
        self.last_emitted_at = self.candidate_since
        return {
            "accepted": True,
            "active_seconds": active_seconds,
        }


class DurationCycleDetector:
    """Measure only stable active intervals after an inactive baseline."""

    def __init__(
        self,
        *,
        active_level: bool,
        start_stable_seconds: float,
        stop_stable_seconds: float,
    ):
        self.active_level = active_level
        self.start_stable_seconds = max(0.0, start_stable_seconds)
        self.stop_stable_seconds = max(0.0, stop_stable_seconds)
        self.stable_value: bool | None = None
        self.candidate_value: bool | None = None
        self.candidate_since = 0.0
        self.armed = False
        self.active_started_at: float | None = None

    def feed(self, value: bool, now: float) -> dict[str, Any] | None:
        value = bool(value)
        if self.stable_value is None:
            self.stable_value = value
            self.candidate_value = value
            self.candidate_since = now
            self.armed = False
            return None

        if value != self.candidate_value:
            self.candidate_value = value
            self.candidate_since = now
            return None

        if value == self.stable_value:
            if (
                value != self.active_level
                and not self.armed
                and self.active_started_at is None
                and now - self.candidate_since >= self.stop_stable_seconds
            ):
                self.armed = True
            return None

        required_stable = (
            self.start_stable_seconds
            if value == self.active_level
            else self.stop_stable_seconds
        )
        if now - self.candidate_since < required_stable:
            return None

        previous = self.stable_value
        self.stable_value = value
        if value == self.active_level:
            if self.armed and previous != self.active_level:
                self.active_started_at = self.candidate_since
                self.armed = False
                return {"event": "started", "started_at": self.active_started_at}
            return None

        self.armed = True
        if previous != self.active_level or self.active_started_at is None:
            return None
        started_at = self.active_started_at
        self.active_started_at = None
        return {
            "event": "stopped",
            "started_at": started_at,
            "ended_at": self.candidate_since,
            "elapsed_seconds": max(0.0, self.candidate_since - started_at),
        }


def run_pulse_mode(config: Config, events: PersistentQueue, stop: threading.Event) -> None:
    pin = DigitalInputDevice(config.gpio_bcm, pull_up=config.pull_up)
    active_level = config.pulse_edge != "falling"
    detector = PulseCycleDetector(
        active_level=active_level,
        min_active_seconds=config.pulse_min_active_seconds,
        rearm_seconds=config.pulse_rearm_seconds,
        max_active_seconds=config.pulse_max_active_seconds,
        min_interval_seconds=config.pulse_min_interval_seconds,
    )
    print(
        "pulse cycle detector started "
        f"edge={config.pulse_edge} initial_value={int(bool(pin.value))} "
        f"poll={config.poll_interval_seconds}s min_active={config.pulse_min_active_seconds}s "
        f"rearm={config.pulse_rearm_seconds}s max_active={config.pulse_max_active_seconds}s "
        f"min_interval={config.pulse_min_interval_seconds}s",
        flush=True,
    )
    while not stop.is_set():
        now = time.monotonic()
        result = detector.feed(bool(pin.value), now)
        if result and result.get("accepted"):
            enqueue_measurement(
                config,
                events,
                delta=1.0,
                extra={
                    "pulse": {
                        "edge": config.pulse_edge,
                        "active_seconds": round(result["active_seconds"], 4),
                        "validated_cycle": True,
                    }
                },
            )
        elif result:
            print(
                f"pulse ignored reason={result['reason']} active={result['active_seconds']:.4f}s",
                flush=True,
            )
        stop.wait(config.poll_interval_seconds)


def run_duration_mode(config: Config, events: PersistentQueue, stop: threading.Event) -> None:
    pin = DigitalInputDevice(config.gpio_bcm, pull_up=config.pull_up)
    active_level = config.duration_active_level == "high"
    detector = DurationCycleDetector(
        active_level=active_level,
        start_stable_seconds=config.duration_start_stable_seconds,
        stop_stable_seconds=config.duration_stop_stable_seconds,
    )
    started_at_utc: str | None = None
    print(
        "duration cycle detector started "
        f"active_level={config.duration_active_level} initial_value={int(bool(pin.value))} "
        f"start_stable={config.duration_start_stable_seconds}s "
        f"stop_stable={config.duration_stop_stable_seconds}s",
        flush=True,
    )
    while not stop.is_set():
        result = detector.feed(bool(pin.value), time.monotonic())
        if result and result["event"] == "started":
            started_at_utc = datetime.now(timezone.utc).isoformat()
            print("duration started", flush=True)
        elif result and result["event"] == "stopped":
            elapsed = result["elapsed_seconds"]
            ended_at = datetime.now(timezone.utc).isoformat()
            if elapsed < config.min_duration_seconds:
                print(f"duration ignored elapsed={elapsed:.3f}s", flush=True)
            else:
                delta = elapsed if config.duration_unit == "seconds" else elapsed / 60.0
                enqueue_measurement(
                    config,
                    events,
                    delta=delta,
                    extra={
                        "duration": {
                            "started_at": started_at_utc,
                            "ended_at": ended_at,
                            "seconds": round(elapsed, 2),
                            "is_final_chunk": True,
                            "validated_cycle": True,
                        }
                    },
                )
                print(f"duration stopped total_elapsed={elapsed:.3f}s", flush=True)
            started_at_utc = None
        stop.wait(config.poll_interval_seconds)


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
