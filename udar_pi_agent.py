#!/usr/bin/env python3
"""
UDAR CRM Raspberry Pi GPIO agent.

Default wiring:
- Physical pin 11 = GPIO17/BCM17 signal input. The machine/button must provide 3.3V pulses here.
- Physical pin 9 = GND/Terra. Without an optocoupler, machine ground and Raspberry Pi
  ground must be common (an optocoupler isolates them; then no common ground is needed).

Never feed 5V, 12V, 24V, or noisy industrial voltage directly into a Raspberry Pi GPIO pin.
Use an optocoupler, relay interface, or level shifter when the machine signal is not clean 3.3V logic.
Wiring checklist and measurement-based tuning: README.md.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NamedTuple

# Ajan sürümü. Her kayıtta İKİ ayrı alan olarak gider:
# - measured_by_version: vuruşu ÖLÇEN ajanın sürümü; ölçüm anında kuyruğa yazılır.
# - agent_version: kaydı GÖNDEREN ajanın sürümü; her gönderimde eklenir.
# Pi internetsizken eski ajanın kuyruğa biriktirdiği kayıt güncellemeden sonra
# yeni ajanla gider: agent_version yeni sürümü, measured_by_version ise boş
# (null) gösterir; yani ölçüm bu alanı bilmeyen, 2026.09.30 öncesi bir ajandan
# kalmıştır. İki alan da yoksa kaydı eski ajan göndermiştir. Daha eski dönem
# kaydın şeklinden okunur, ama önce counter.mode'a bakılır (her sürüm yazar):
# 'pulse' alanının yokluğu YALNIZ counter.mode == 'pulse' kayıtta 391b844 öncesi
# (when_pressed) ajan demektir; süre kaydında 'pulse' alanı hiçbir sürümde
# olmaz. Karar sırası: README "Kaydı hangi ajan ölçtü?". Davranışı
# değiştiren her değişiklikte artırılır (biçim: YIL.AY.GÜN, aynı gün ikinci
# sürüm için .2).
AGENT_VERSION = "2026.09.30"

try:
    import requests
except ImportError as exc:  # pragma: no cover
    raise SystemExit("python3-requests is required. Run: sudo apt install python3-requests") from exc

try:
    from gpiozero import DigitalInputDevice
except ImportError as exc:  # pragma: no cover
    raise SystemExit("python3-gpiozero is required. Run: sudo apt install python3-gpiozero") from exc


def env(name: str, default: str = "") -> str:
    """Ortam değişkenini okur; BOŞ değer, anahtar hiç yokmuş gibi öndeğere düşer.

    ``os.environ.get(name, default)`` anahtar varsa ama değeri boşsa öndeğeri
    değil ``""`` döndürür. Env dosyasında ``UDAR_POLL_INTERVAL_SECONDS=`` gibi
    boş bırakılmış bir satır, sayısal alanlarda ``float("")`` ile ajanı AÇILIŞTA
    çökertiyordu; ``UDAR_QUEUE_DB=`` boş kalınca kuyruk dosyası çalışma dizinine
    düşüyordu. Boş bırakılmış bir ayar "ayarlanmamış" demektir.
    """
    value = os.environ.get(name, "").strip()
    return value if value else default


ENV_DOSYASI = "/etc/udar-pi-agent.env"


def _sayi_hatasi(name: str, value: str, beklenen: str) -> SystemExit:
    return SystemExit(
        f"AYAR HATASI: {ENV_DOSYASI} icinde {name} {beklenen} olmali, su an: {value!r}\n"
        f"Duzeltmek icin Pi'nin terminalinde:  cd ~/udar-pi-agent && bash install.sh --configure\n"
        f"ya da elle:  sudo nano {ENV_DOSYASI}"
    )


def float_env(name: str, default: str) -> float:
    """Sayi ayari. Bozuk deger TRACEBACK ile degil, ne yapilacagini soyleyen mesajla durur."""
    value = env(name, default)
    try:
        return float(value)
    except ValueError:
        raise _sayi_hatasi(name, value, "sayi (ornek: " + default + ")") from None


def int_env(name: str, default: str) -> int:
    value = env(name, default)
    try:
        return int(value)
    except ValueError:
        raise _sayi_hatasi(name, value, "tam sayi (ornek: " + default + ")") from None


def bool_env(name: str, default: str = "false") -> bool:
    return env(name, default).lower() in {"1", "true", "yes", "on"}


# Vuruş süzgecinin öndeğerleri ürün tarafsızdır; makineye göre ayar ÖLÇÜLEREK
# yapılır (diagnose_gpio.py, README "Ayarı ölçerek bulmak"). Öndeğer değişirse
# sahadaki her kurulumun sayımı değişir.
DEFAULT_PULSE_MIN_INTERVAL_SECONDS = 0.20
# En kısa gerçek çevrimi bundan kısa makinede tek tek vuruş saymak hataya
# açıktır (öndeğer süzgeçle kabul edilen en kısa çevrim ~0,22 sn).
FAST_CYCLE_SECONDS = 0.25


def min_interval_suggestion(shortest_cycle_seconds: float) -> float:
    """``UDAR_PULSE_MIN_INTERVAL_SECONDS`` önerisi: en kısa GERÇEK çevrimin yarısı.

    Yarısı 0,01'e aşağı yuvarlanır. Yarısı öndeğerin (0,20) altına düşüyorsa ve
    çevrim ``FAST_CYCLE_SECONDS``'tan uzunsa öndeğer kalır: süzgeç zaten ~0,22
    sn'nin altındaki çevrimi kabul etmez, daha düşük değer bir şey kazandırmaz.
    Çevrim bundan da kısaysa yarısı döner (öneri çevrimi asla aşmaz; aşarsa
    gerçek vuruşlar sayılmazdı). Çevrim bilinmiyorsa (0) öndeğer döner.

    install.sh aynı kuralı awk ile uygular (ARALIK_ONERISI_AWK); test ikisini
    karşılaştırır — biri değişirse öbürü de değişmeli.
    """
    if not shortest_cycle_seconds or shortest_cycle_seconds <= 0:
        return DEFAULT_PULSE_MIN_INTERVAL_SECONDS
    half = math.floor(round(shortest_cycle_seconds * 100 / 2, 6)) / 100
    if half < DEFAULT_PULSE_MIN_INTERVAL_SECONDS and shortest_cycle_seconds > FAST_CYCLE_SECONDS:
        return DEFAULT_PULSE_MIN_INTERVAL_SECONDS
    return half


def pulse_active_level(edge: str) -> bool:
    """Vuruş modunda 'aktif' sayılan pin seviyesi (True = 1).

    rising: boşta 0, vuruşta 1. falling: boşta 1, vuruşta 0.
    both: ESKİ bir ayar ve rising ile AYNI çalışır. Belge bir dönem "her 0/1
    değişimini say" diyordu, ama kod hiçbir zaman öyle saymadı: her zaman tam
    bir boş -> aktif -> boş çevrimini 1 adet saydı. Kod belgeye uydurulsaydı
    sahada 'both' ayarlı cihazlar bir güncellemeyle sessizce ÇİFT saymaya
    başlardı (bir çevrimde iki değişim var). Davranış korundu, belge
    düzeltildi; kurulum sihirbazı artık 'both' önermiyor.
    """
    return edge != "falling"


@dataclass(frozen=True)
class Config:
    crm_url: str
    device_token: str
    gpio_bcm: int = 17
    pull_up: bool = False
    timeout: float = 5.0
    queue_db: Path = Path("/var/lib/udar-pi-agent/machine_events.sqlite3")
    measurement_mode: str = "pulse"
    pulse_edge: str = "rising"
    poll_interval_seconds: float = 0.002
    pulse_min_active_seconds: float = 0.02
    pulse_rearm_seconds: float = 0.20
    pulse_max_active_seconds: float = 10.0
    pulse_min_interval_seconds: float = DEFAULT_PULSE_MIN_INTERVAL_SECONDS
    duration_unit: str = "seconds"
    min_duration_seconds: float = 0.2
    duration_start_stable_seconds: float = 0.20
    duration_stop_stable_seconds: float = 0.20
    duration_dropout_grace_seconds: float = 1.50
    duration_dropout_log_interval_seconds: float = 30.0
    duration_active_level: str = "high"
    daily_reset: bool = True
    line_id: str = ""
    station_code: str = ""
    operator_id: str = ""
    note: str = "GPIO event"
    send_retry_base_seconds: float = 5.0
    send_retry_validation_seconds: float = 60.0
    send_retry_max_seconds: float = 300.0

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
        gpio_bcm=int_env("UDAR_GPIO_BCM", "17"),
        # UDAR_BOUNCE_SECONDS BİLEREK okunmaz. Temmuz 2026'dan (391b844) beri
        # kullanılmıyor: daha eski ajanda gpiozero Button'ın bounce_time'ıydı;
        # 391b844 sayımı PulseCycleDetector'a taşıyınca değer yalnız okunur
        # oldu, pine hiç verilmedi. Bozuk değeri ise ajanı açılışta
        # çökertiyordu (21 Eylül). Süzgeç UDAR_PULSE_* ayarlarıdır. Eski
        # dosyada kalmışsa main() hatırlatır.
        pull_up=bool_env("UDAR_PULL_UP"),
        timeout=float_env("UDAR_HTTP_TIMEOUT", "5"),
        queue_db=Path(env("UDAR_QUEUE_DB", "/var/lib/udar-pi-agent/machine_events.sqlite3")),
        measurement_mode=mode,
        pulse_edge=pulse_edge,
        poll_interval_seconds=float_env("UDAR_POLL_INTERVAL_SECONDS", "0.002"),
        pulse_min_active_seconds=float_env("UDAR_PULSE_MIN_ACTIVE_SECONDS", "0.02"),
        pulse_rearm_seconds=float_env("UDAR_PULSE_REARM_SECONDS", "0.20"),
        pulse_max_active_seconds=float_env("UDAR_PULSE_MAX_ACTIVE_SECONDS", "10.0"),
        pulse_min_interval_seconds=float_env("UDAR_PULSE_MIN_INTERVAL_SECONDS", "0.20"),
        duration_unit=unit,
        min_duration_seconds=float_env("UDAR_MIN_DURATION_SECONDS", "0.2"),
        duration_start_stable_seconds=float_env("UDAR_DURATION_START_STABLE_SECONDS", "0.20"),
        duration_stop_stable_seconds=float_env("UDAR_DURATION_STOP_STABLE_SECONDS", "0.20"),
        duration_dropout_grace_seconds=float_env("UDAR_DURATION_DROPOUT_GRACE_SECONDS", "1.50"),
        duration_dropout_log_interval_seconds=float(
            env("UDAR_DURATION_DROPOUT_LOG_INTERVAL_SECONDS", "30.0")
        ),
        duration_active_level=duration_active_level,
        daily_reset=bool_env("UDAR_DAILY_RESET", "true"),
        line_id=env("UDAR_LINE_ID"),
        station_code=env("UDAR_STATION_CODE"),
        operator_id=env("UDAR_OPERATOR_ID"),
        note=env("UDAR_NOTE", "GPIO event"),
        send_retry_base_seconds=float_env("UDAR_SEND_RETRY_BASE_SECONDS", "5"),
        send_retry_validation_seconds=float_env("UDAR_SEND_RETRY_VALIDATION_SECONDS", "60"),
        send_retry_max_seconds=float_env("UDAR_SEND_RETRY_MAX_SECONDS", "300"),
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


def lock_path_for(queue_db: Path) -> Path:
    return queue_db.with_name(queue_db.name + ".lock")


def acquire_single_instance_lock(queue_db: Path):
    """Aynı Pi'de ikinci bir ajanın çalışmasını engeller.

    İki ajan aynı pini okuyup aynı tokenla gönderirse her vuruş iki kez sayılır
    (eski kurulumdan kalma ikinci servis, elle başlatılmış deneme). Kilit
    dosyası kuyruk DB'sinin yanındadır: aynı kuyruğu kullanan iki süreç de
    böylece dışlanır. Kilit çekirdekte tutulur: ajan çökse ya da öldürülse bile
    kendiliğinden bırakılır, dosyayı silmek gerekmez. Dönen dosya nesnesi süreç
    boyunca açık tutulmalıdır; kapanırsa kilit bırakılır.
    """
    path = lock_path_for(queue_db)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.seek(0)
        owner = handle.read().strip()
        handle.close()
        raise SystemExit(
            f"AJAN ZATEN CALISIYOR: {path} kilidi baska bir surecte"
            + (f" (pid {owner})" if owner else "")
            + ".\nAyni makineyi iki ajan okursa her vurus iki kez sayilir; bu kopya baslatilmadi.\n"
            "Kontrol:  pgrep -af udar_pi_agent   (tek satir gorunmeli)\n"
            "Servisi yeniden baslatmak icin:  sudo systemctl restart udar-pi-agent"
        ) from None
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    return handle


TIMESYNC_MARKER = Path("/run/systemd/timesync/synchronized")
TIMEDATECTL_COMMAND = ("timedatectl", "show", "--property=NTPSynchronized", "--value")


class ClockSyncMonitor:
    """Pi saatinin internetten eşitlenip eşitlenmediğini izler.

    Pi'de pilli saat (RTC) yoktur: açılışta saat son kapanıştaki değerden
    başlar ve internet gelip eşitlenene kadar geride kalır. O arada ölçülen
    vuruşun ``timestamp``'i yanlış olabilir. Ajan bunu her kayda
    ``clock_synced`` olarak yazar: true/false, bilinmiyorsa null.

    Servis saatin eşitlenmesini BEKLEMEZ (bkz. udar-pi-agent.service): internet
    yokken de vuruşlar sayılıp kuyruğa yazılmalı. Kontrol GPIO döngüsünü de
    bekletmez; ayrı iş parçacığında yapılır, döngü yalnız son değeri okur.

    Saat bu açılışta bir kez eşitlendiyse değer True kalır (yapışkan): uzun bir
    internet kesintisinde saat saniyeler düzeyinde kayar, zamanlar güvenilir
    kalır. Önce systemd-timesyncd'nin işaret dosyasına bakılır (ucuz); yoksa
    (chrony/ntpd) ``timedatectl``'e sorulur.
    """

    def __init__(
        self,
        *,
        marker: Path = TIMESYNC_MARKER,
        command: tuple[str, ...] = TIMEDATECTL_COMMAND,
        interval_seconds: float = 30.0,
    ):
        self.marker = Path(marker)
        self.command = tuple(command)
        self.interval_seconds = interval_seconds
        self.synced: bool | None = None

    def check(self) -> bool | None:
        if self.synced:
            return True
        try:
            if self.marker.exists():
                self.synced = True
                return True
        except OSError:
            pass
        try:
            result = subprocess.run(self.command, capture_output=True, text=True, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            return self.synced
        answer = result.stdout.strip().lower()
        if result.returncode == 0 and answer in {"yes", "no"}:
            self.synced = answer == "yes"
        return self.synced

    def run(self, stop: threading.Event) -> None:
        warned = False
        while not stop.is_set():
            if self.check():
                print("clock synchronized (clock_synced=true)", flush=True)
                return
            if self.synced is False and not warned:
                print(
                    "UYARI: Pi saati henuz internetten eslenmedi; kayitlar clock_synced=false ile gidiyor. "
                    "Kontrol: timedatectl",
                    flush=True,
                )
                warned = True
            stop.wait(self.interval_seconds)


def read_boot_id() -> str:
    """Bu açılışın kimliği; Pi yeniden başlayınca değişir. Okunamazsa ''."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return ""


class QueuedEvent(NamedTuple):
    """Kuyruğun başındaki, gönderilmeye hazır kayıt."""

    row_id: int
    payload: dict[str, Any]
    attempts: int  # şimdiye kadarki BAŞARISIZ deneme sayısı
    queued_at: str  # kuyruğa girdiği an, cihaz saatiyle (UTC ISO)
    # Kuyruğa girişten bu yana geçen süre, cihazın MONOTON saatiyle: saat
    # sonradan internetten düzeltilse (açılışta sıçrasa) bile doğrudur. Pi arada
    # yeniden başladıysa ya da kayıt eski sürümden kaldıysa None.
    queue_age_seconds: float | None


class PersistentQueue:
    def __init__(self, db_path: Path, *, boot_id: str | None = None, monotonic=time.monotonic):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        # time.monotonic() Linux'ta açılıştan beri sayar (CLOCK_MONOTONIC):
        # ajan yeniden başlasa da aynı açılışta tutarlıdır; açılış kimliği
        # değiştiyse karşılaştırılmaz.
        self.boot_id = read_boot_id() if boot_id is None else boot_id
        self._monotonic = monotonic
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT UNIQUE NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    next_attempt_at TEXT NOT NULL DEFAULT '',
                    queued_monotonic REAL,
                    boot_id TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._ensure_events_column(conn, "next_attempt_at", "TEXT NOT NULL DEFAULT ''")
            # Sahadaki eski kuyruk dosyaları bu iki sütunu taşımaz.
            self._ensure_events_column(conn, "queued_monotonic", "REAL")
            self._ensure_events_column(conn, "boot_id", "TEXT NOT NULL DEFAULT ''")
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

    def _ensure_events_column(self, conn: sqlite3.Connection, name: str, definition: str) -> None:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(events)").fetchall()}
        if name not in columns:
            conn.execute(f"ALTER TABLE events ADD COLUMN {name} {definition}")

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
                """
                INSERT OR IGNORE INTO events
                    (idempotency_key, payload, created_at, next_attempt_at, queued_monotonic, boot_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (payload["idempotency_key"], body, now, now, self._monotonic(), self.boot_id),
            )
            conn.execute(
                "INSERT INTO event_log (idempotency_key, payload, status, created_at) VALUES (?, ?, ?, ?)",
                (payload["idempotency_key"], body, "queued", now),
            )

    def peek(self) -> QueuedEvent | None:
        now = datetime.now(timezone.utc).isoformat()
        with self.lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, payload, attempts, created_at, queued_monotonic, boot_id
                FROM events
                WHERE next_attempt_at = '' OR next_attempt_at <= ?
                ORDER BY id
                LIMIT 1
                """,
                (now,),
            ).fetchone()
        if not row:
            return None
        age: float | None = None
        if row[4] is not None and self.boot_id and row[5] == self.boot_id:
            elapsed = self._monotonic() - float(row[4])
            if elapsed >= 0:
                age = elapsed
        return QueuedEvent(int(row[0]), json.loads(row[1]), int(row[2]), str(row[3]), age)

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

    def fail(self, row_id: int, error: str, retry_after_seconds: float) -> None:
        next_attempt_at = (
            datetime.now(timezone.utc) + timedelta(seconds=max(1.0, retry_after_seconds))
        ).isoformat()
        with self.lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE events
                SET attempts = attempts + 1, last_error = ?, next_attempt_at = ?
                WHERE id = ?
                """,
                (error[:500], next_attempt_at, row_id),
            )


def retry_delay(config: Config, *, status_code: int | None, attempts: int, retry_after: str = "") -> float:
    if retry_after:
        try:
            return min(config.send_retry_max_seconds, max(1.0, float(retry_after)))
        except ValueError:
            pass
    if status_code in {400, 403, 404, 409, 422}:
        base = config.send_retry_validation_seconds
    elif status_code == 429:
        base = max(config.send_retry_base_seconds, 30.0)
    else:
        base = config.send_retry_base_seconds
    multiplier = 2 ** min(max(0, attempts), 6)
    return min(config.send_retry_max_seconds, max(1.0, base * multiplier))


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
        # Ölçen ajanın sürümü: kuyruğa ölçüm anında yazılır, gönderen ajan
        # değişse de (güncelleme) değişmez. Gönderenin sürümü agent_version.
        "measured_by_version": AGENT_VERSION,
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


def transport_payload(item: QueuedEvent, *, now: datetime | None = None) -> dict[str, Any]:
    """Kuyruktaki ölçümü, bu gönderim denemesinin bilgileriyle birlikte döndürür.

    Kuyruktaki kayıt değişmez; bu alanlar her denemede yeniden hesaplanır:
    - agent_version: GÖNDEREN ajanın sürümü (alan yoksa eski ajan gönderdi).
    - measured_by_version: ÖLÇEN ajanın sürümü; kuyruktaki kayıttan gelir,
      burada yalnız yoksa null yazılır: kayıt bu alanı bilmeyen eski bir
      ajanın kuyruğundan kalmıştır. Gönderen sürümle karıştırılmaz.
    - queued_at: kuyruğa girdiği an (cihaz saati).
    - sent_at: bu gönderimin anı (cihaz saati). Sunucu ``sunucu_saati - sent_at``
      ile Pi saatinin sapmasını bulup ölçüm zamanını düzeltebilir.
    - attempts: bu gönderim kaçıncı deneme (ilk gönderimde 1).
    - queue_age_seconds: ölçümden bu gönderime geçen süre, monoton saatle
      (Pi saati arada sıçrasa bile doğru). Pi yeniden başladıysa gönderilmez.
    Kayıt tekrarını ``idempotency_key`` önler; o değişmez.
    """
    body = dict(item.payload)
    body["agent_version"] = AGENT_VERSION
    body.setdefault("measured_by_version", None)
    body["queued_at"] = item.queued_at
    body["sent_at"] = (now or datetime.now(timezone.utc)).isoformat()
    body["attempts"] = item.attempts + 1
    if item.queue_age_seconds is not None:
        body["queue_age_seconds"] = round(item.queue_age_seconds, 3)
    return body


def sender_loop(config: Config, events: PersistentQueue, stop: threading.Event) -> None:
    session = requests.Session()
    while not stop.is_set():
        item = events.peek()
        if not item:
            stop.wait(0.5)
            continue
        payload = transport_payload(item)
        try:
            response = session.post(config.endpoint, json=payload, timeout=config.timeout)
            if 200 <= response.status_code < 300:
                events.mark_sent(item.row_id, payload, response.status_code, response.text)
                total = (payload.get("counter") or {}).get("total")
                print(
                    f"sent {payload['idempotency_key']} total={total} attempts={payload['attempts']}",
                    flush=True,
                )
            else:
                error = f"HTTP {response.status_code}: {response.text[:300]}"
                delay = retry_delay(
                    config,
                    status_code=response.status_code,
                    attempts=item.attempts,
                    retry_after=response.headers.get("Retry-After", ""),
                )
                events.fail(item.row_id, error, delay)
                print(f"{error} retry_in={delay:.0f}s", file=sys.stderr, flush=True)
                stop.wait(min(delay, 30.0))
        except requests.RequestException as exc:
            delay = retry_delay(config, status_code=None, attempts=item.attempts)
            events.fail(item.row_id, str(exc), delay)
            print(f"send failed: {exc} retry_in={delay:.0f}s", file=sys.stderr, flush=True)
            stop.wait(min(delay, 30.0))


def enqueue_measurement(
    config: Config,
    events: PersistentQueue,
    *,
    delta: float,
    extra: dict[str, Any] | None = None,
    clock_synced: bool | None = None,
) -> None:
    sequence, total, day = events.next_daily_values(delta, reset=config.daily_reset)
    payload = build_payload(
        config,
        sequence=sequence,
        delta=delta,
        total=total,
        measured_at=datetime.now(timezone.utc).isoformat(),
        # clock_synced ÖLÇÜM anındaki durumdur: timestamp'e güvenilir mi?
        extra={"counter_day": day, "clock_synced": clock_synced, **(extra or {})},
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

        started_at = self.active_started_at
        ended_at = self.candidate_since
        active_seconds = max(0.0, ended_at - started_at)
        self.active_started_at = None
        if active_seconds < self.min_active_seconds:
            return None
        # started_at/ended_at: aktifliğin başı ve sonu (monoton saat). Ajan
        # kullanmaz; diagnose_gpio.py çevrim ve bekleme sürelerini buradan ölçer.
        timing = {"active_seconds": active_seconds, "started_at": started_at, "ended_at": ended_at}
        if self.max_active_seconds > 0 and active_seconds > self.max_active_seconds:
            return {"accepted": False, "reason": "active_too_long", **timing}
        if ended_at - self.last_emitted_at < self.min_interval_seconds:
            return {"accepted": False, "reason": "rate_limited", **timing}
        self.last_emitted_at = ended_at
        return {"accepted": True, **timing}


class DurationCycleDetector:
    """Measure only stable active intervals after an inactive baseline."""

    def __init__(
        self,
        *,
        active_level: bool,
        start_stable_seconds: float,
        stop_stable_seconds: float,
        dropout_grace_seconds: float = 0.0,
    ):
        self.active_level = active_level
        self.start_stable_seconds = max(0.0, start_stable_seconds)
        self.stop_stable_seconds = max(0.0, stop_stable_seconds)
        self.dropout_grace_seconds = max(0.0, dropout_grace_seconds)
        self.stable_value: bool | None = None
        self.candidate_value: bool | None = None
        self.candidate_since = 0.0
        self.armed = False
        self.active_started_at: float | None = None
        self.ignored_dropout_count = 0
        self.ignored_dropout_seconds = 0.0

    @property
    def required_stop_seconds(self) -> float:
        return max(self.stop_stable_seconds, self.dropout_grace_seconds)

    def feed(self, value: bool, now: float) -> dict[str, Any] | None:
        value = bool(value)
        if self.stable_value is None:
            self.stable_value = value
            self.candidate_value = value
            self.candidate_since = now
            self.armed = False
            return None

        if value != self.candidate_value:
            ignored_dropout: dict[str, Any] | None = None
            if (
                self.stable_value == self.active_level
                and self.active_started_at is not None
                and self.candidate_value != self.active_level
                and value == self.active_level
            ):
                dropout_seconds = max(0.0, now - self.candidate_since)
                if dropout_seconds < self.required_stop_seconds:
                    self.ignored_dropout_count += 1
                    self.ignored_dropout_seconds += dropout_seconds
                    ignored_dropout = {
                        "event": "dropout_ignored",
                        "dropout_seconds": dropout_seconds,
                    }
            self.candidate_value = value
            self.candidate_since = now
            return ignored_dropout

        if value == self.stable_value:
            if (
                value != self.active_level
                and not self.armed
                and self.active_started_at is None
                and now - self.candidate_since >= self.required_stop_seconds
            ):
                self.armed = True
            return None

        required_stable = (
            self.start_stable_seconds
            if value == self.active_level
            else self.required_stop_seconds
        )
        if now - self.candidate_since < required_stable:
            return None

        previous = self.stable_value
        self.stable_value = value
        if value == self.active_level:
            if self.armed and previous != self.active_level:
                self.active_started_at = self.candidate_since
                self.armed = False
                self.ignored_dropout_count = 0
                self.ignored_dropout_seconds = 0.0
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
            "ignored_dropout_count": self.ignored_dropout_count,
            "ignored_dropout_seconds": self.ignored_dropout_seconds,
        }


def run_pulse_mode(
    config: Config,
    events: PersistentQueue,
    stop: threading.Event,
    clock: ClockSyncMonitor | None = None,
) -> None:
    pin = DigitalInputDevice(config.gpio_bcm, pull_up=config.pull_up)
    active_level = pulse_active_level(config.pulse_edge)
    if config.pulse_edge == "both":
        print(
            "NOT: UDAR_PULSE_EDGE=both, 'rising' ile ayni calisir: her tam 0->1->0 cevrimi 1 adet "
            "sayilir, her degisim ayri sayilmaz. Karisiklik olmasin diye 'rising' yazilabilir.",
            flush=True,
        )
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
        result = detector.feed(bool(pin.value), time.monotonic())
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
                clock_synced=clock.synced if clock else None,
            )
        elif result:
            print(
                f"pulse ignored reason={result['reason']} active={result['active_seconds']:.4f}s",
                flush=True,
            )
        stop.wait(config.poll_interval_seconds)


def run_duration_mode(
    config: Config,
    events: PersistentQueue,
    stop: threading.Event,
    clock: ClockSyncMonitor | None = None,
) -> None:
    pin = DigitalInputDevice(config.gpio_bcm, pull_up=config.pull_up)
    active_level = config.duration_active_level == "high"
    detector = DurationCycleDetector(
        active_level=active_level,
        start_stable_seconds=config.duration_start_stable_seconds,
        stop_stable_seconds=config.duration_stop_stable_seconds,
        dropout_grace_seconds=config.duration_dropout_grace_seconds,
    )
    started_at_utc: str | None = None
    dropout_log_count = 0
    dropout_log_seconds = 0.0
    last_dropout_log_at = 0.0

    def flush_dropout_log(now: float, *, force: bool = False) -> None:
        nonlocal dropout_log_count, dropout_log_seconds, last_dropout_log_at
        if not dropout_log_count:
            return
        log_interval = max(1.0, config.duration_dropout_log_interval_seconds)
        if not force and last_dropout_log_at and now - last_dropout_log_at < log_interval:
            return
        print(
            "duration input noise filtered "
            f"count={dropout_log_count} low_total={dropout_log_seconds:.3f}s",
            flush=True,
        )
        dropout_log_count = 0
        dropout_log_seconds = 0.0
        last_dropout_log_at = now

    print(
        "duration cycle detector started "
        f"active_level={config.duration_active_level} initial_value={int(bool(pin.value))} "
        f"start_stable={config.duration_start_stable_seconds}s "
        f"stop_stable={config.duration_stop_stable_seconds}s "
        f"dropout_grace={config.duration_dropout_grace_seconds}s",
        flush=True,
    )
    while not stop.is_set():
        now = time.monotonic()
        result = detector.feed(bool(pin.value), now)
        if result and result["event"] == "started":
            started_at_utc = datetime.now(timezone.utc).isoformat()
            print("duration started", flush=True)
        elif result and result["event"] == "dropout_ignored":
            dropout_log_count += 1
            dropout_log_seconds += result["dropout_seconds"]
            flush_dropout_log(now)
        elif result and result["event"] == "stopped":
            flush_dropout_log(now, force=True)
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
                            "ignored_dropout_count": result["ignored_dropout_count"],
                            "ignored_dropout_seconds": round(result["ignored_dropout_seconds"], 3),
                            "dropout_grace_seconds": config.duration_dropout_grace_seconds,
                        }
                    },
                    clock_synced=clock.synced if clock else None,
                )
                print(f"duration stopped total_elapsed={elapsed:.3f}s", flush=True)
            started_at_utc = None
        stop.wait(config.poll_interval_seconds)
    flush_dropout_log(time.monotonic(), force=True)


def main() -> int:
    config = load_config()
    # Kuyruğa ve pine dokunmadan ÖNCE: ikinci kopya hiçbir şey okumadan çıkar.
    instance_lock = acquire_single_instance_lock(config.queue_db)  # noqa: F841 (süreç boyunca açık kalmalı)
    events = PersistentQueue(config.queue_db)
    stop = threading.Event()
    clock = ClockSyncMonitor()

    def handle_signal(signum, frame):  # noqa: ARG001
        stop.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    threading.Thread(target=clock.run, args=(stop,), daemon=True, name="clock-sync").start()
    sender = threading.Thread(target=sender_loop, args=(config, events, stop), daemon=True)
    sender.start()

    print(
        f"UDAR Pi agent {AGENT_VERSION} started. mode={config.measurement_mode} BCM GPIO{config.gpio_bcm} signal, "
        f"physical pin 9 GND/Terra. pull_up={str(config.pull_up).lower()} Endpoint={config.endpoint}",
        flush=True,
    )
    if env("UDAR_BOUNCE_SECONDS"):
        print(
            "NOT: UDAR_BOUNCE_SECONDS Temmuz 2026'dan beri kullanilmiyor (daha eski ajanda "
            "gpiozero Button bounce_time idi); ayar dosyasindan silinebilir. "
            "Sinyal suzgeci UDAR_PULSE_* ayarlaridir.",
            flush=True,
        )
    if config.measurement_mode == "duration":
        run_duration_mode(config, events, stop, clock)
    else:
        run_pulse_mode(config, events, stop, clock)

    sender.join(timeout=3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
