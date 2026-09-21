#!/usr/bin/env python3
import sys
import types
import unittest

requests_module = types.ModuleType("requests")
requests_module.RequestException = Exception
requests_module.Session = object
sys.modules.setdefault("requests", requests_module)

gpiozero_module = types.ModuleType("gpiozero")
gpiozero_module.DigitalInputDevice = object
sys.modules.setdefault("gpiozero", gpiozero_module)

from udar_pi_agent import DurationCycleDetector, PulseCycleDetector


class PulseCycleDetectorTests(unittest.TestCase):
    def detector(self):
        return PulseCycleDetector(
            active_level=True,
            min_active_seconds=0.02,
            rearm_seconds=0.20,
            max_active_seconds=2.0,
            min_interval_seconds=0.20,
        )

    def test_constant_values_do_not_emit(self):
        detector = self.detector()
        for index in range(1000):
            self.assertIsNone(detector.feed(False, index / 100))

    def test_complete_stable_cycle_emits_once(self):
        detector = self.detector()
        detector.feed(False, 0.0)
        detector.feed(False, 0.3)
        detector.feed(True, 0.31)
        detector.feed(True, 0.34)
        detector.feed(False, 0.50)
        result = detector.feed(False, 0.71)
        self.assertTrue(result["accepted"])
        self.assertAlmostEqual(result["active_seconds"], 0.19)
        self.assertIsNone(detector.feed(False, 1.0))

    def test_starting_high_is_not_counted(self):
        detector = self.detector()
        detector.feed(True, 0.0)
        detector.feed(True, 1.0)
        detector.feed(False, 1.1)
        self.assertIsNone(detector.feed(False, 1.4))

    def test_short_noise_is_not_counted(self):
        detector = self.detector()
        detector.feed(False, 0.0)
        detector.feed(False, 0.3)
        detector.feed(True, 0.31)
        detector.feed(False, 0.315)
        self.assertIsNone(detector.feed(False, 0.6))

    def test_stuck_high_is_rejected_after_release(self):
        detector = self.detector()
        detector.feed(False, 0.0)
        detector.feed(False, 0.3)
        detector.feed(True, 0.31)
        detector.feed(True, 0.34)
        detector.feed(False, 3.0)
        result = detector.feed(False, 3.3)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "active_too_long")


class DurationCycleDetectorTests(unittest.TestCase):
    def detector(self):
        return DurationCycleDetector(
            active_level=True,
            start_stable_seconds=0.20,
            stop_stable_seconds=0.20,
            dropout_grace_seconds=0.20,
        )

    def test_chatter_does_not_start_or_stop(self):
        detector = self.detector()
        detector.feed(False, 0.0)
        detector.feed(True, 0.10)
        detector.feed(False, 0.15)
        detector.feed(True, 0.20)
        detector.feed(False, 0.25)
        self.assertIsNone(detector.feed(False, 0.50))

    def test_stable_interval_is_reported_once(self):
        detector = self.detector()
        detector.feed(False, 0.0)
        detector.feed(False, 0.3)
        detector.feed(True, 1.0)
        started = detector.feed(True, 1.21)
        self.assertEqual(started["event"], "started")
        detector.feed(False, 11.0)
        stopped = detector.feed(False, 11.21)
        self.assertEqual(stopped["event"], "stopped")
        self.assertAlmostEqual(stopped["elapsed_seconds"], 10.0)
        self.assertIsNone(detector.feed(False, 12.0))

    def test_starting_high_requires_inactive_baseline(self):
        detector = self.detector()
        detector.feed(True, 0.0)
        detector.feed(True, 1.0)
        detector.feed(False, 2.0)
        self.assertIsNone(detector.feed(False, 2.21))
        detector.feed(True, 3.0)
        started = detector.feed(True, 3.21)
        self.assertEqual(started["event"], "started")

    def test_short_low_dropout_does_not_split_duration(self):
        detector = DurationCycleDetector(
            active_level=True,
            start_stable_seconds=0.20,
            stop_stable_seconds=0.20,
            dropout_grace_seconds=1.50,
        )
        detector.feed(False, 0.0)
        detector.feed(False, 1.6)
        detector.feed(True, 2.0)
        self.assertEqual(detector.feed(True, 2.21)["event"], "started")

        detector.feed(False, 5.0)
        self.assertIsNone(detector.feed(False, 5.5))
        ignored = detector.feed(True, 5.6)
        self.assertEqual(ignored["event"], "dropout_ignored")
        self.assertAlmostEqual(ignored["dropout_seconds"], 0.6)

        detector.feed(False, 10.0)
        stopped = detector.feed(False, 11.51)
        self.assertEqual(stopped["event"], "stopped")
        self.assertAlmostEqual(stopped["elapsed_seconds"], 8.0)
        self.assertEqual(stopped["ignored_dropout_count"], 1)
        self.assertAlmostEqual(stopped["ignored_dropout_seconds"], 0.6)

    def test_low_longer_than_grace_is_real_stop(self):
        detector = DurationCycleDetector(
            active_level=True,
            start_stable_seconds=0.20,
            stop_stable_seconds=0.20,
            dropout_grace_seconds=1.50,
        )
        detector.feed(False, 0.0)
        detector.feed(False, 1.6)
        detector.feed(True, 2.0)
        detector.feed(True, 2.21)
        detector.feed(False, 5.0)
        stopped = detector.feed(False, 6.51)
        self.assertEqual(stopped["event"], "stopped")
        self.assertAlmostEqual(stopped["elapsed_seconds"], 3.0)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Gönderim kuyruğu ve yeniden deneme beklemesi
#
# Bu sürümün asıl yeniliği bu: eski sürümde gönderilemeyen bir kayıt kuyruğun
# başında kalıyor, ARKASINDAKİ BÜTÜN KAYITLARI bloke ediyor ve beklemeden
# durmadan yeniden deneniyordu (sunucuya yük). Aşağıdaki testler bunu kilitler.
# ---------------------------------------------------------------------------

import sqlite3
import tempfile
from pathlib import Path

from udar_pi_agent import Config, PersistentQueue, retry_delay


def _config():
    return Config(crm_url="https://ornek.test", device_token="test")


class RetryDelayTests(unittest.TestCase):
    def test_gecersiz_kayit_uzun_bekler(self):
        # 400/403/404/409/422: sunucu "bu kayıt böyle kabul edilmez" dedi.
        # Hemen yeniden denemek aynı cevabı alır; 60 sn taban.
        self.assertEqual(retry_delay(_config(), status_code=400, attempts=0), 60.0)
        self.assertEqual(retry_delay(_config(), status_code=422, attempts=1), 120.0)

    def test_gecici_hata_kisa_baslar_ve_katlanir(self):
        self.assertEqual(retry_delay(_config(), status_code=500, attempts=0), 5.0)
        self.assertEqual(retry_delay(_config(), status_code=500, attempts=1), 10.0)
        self.assertEqual(retry_delay(_config(), status_code=None, attempts=2), 20.0)

    def test_bekleme_ust_sinirda_kapanir(self):
        # 5 × 2^6 = 320 olurdu; üst sınır 300.
        self.assertEqual(retry_delay(_config(), status_code=500, attempts=50), 300.0)

    def test_hiz_siniri_en_az_otuz_saniye(self):
        self.assertEqual(retry_delay(_config(), status_code=429, attempts=0), 30.0)

    def test_sunucunun_retry_after_basligina_uyulur(self):
        self.assertEqual(retry_delay(_config(), status_code=503, attempts=0, retry_after="12"), 12.0)
        # Sunucu makul olmayan bir süre isterse üst sınır geçerli.
        self.assertEqual(retry_delay(_config(), status_code=503, attempts=0, retry_after="9999"), 300.0)

    def test_bozuk_retry_after_hesaplanan_sureye_duser(self):
        self.assertEqual(retry_delay(_config(), status_code=500, attempts=0, retry_after="yarin"), 5.0)


class PersistentQueueTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "kuyruk.sqlite3"

    def tearDown(self):
        self._tmp.cleanup()

    def test_reddedilen_kayit_arkasindakileri_bloke_etmez(self):
        kuyruk = PersistentQueue(self.db)
        kuyruk.put({"idempotency_key": "A"})
        kuyruk.put({"idempotency_key": "B"})

        row_id, payload, _ = kuyruk.peek()
        self.assertEqual(payload["idempotency_key"], "A")

        kuyruk.fail(row_id, "400 geçersiz kayıt", retry_after_seconds=60)

        # A 60 sn ertelendi; sıradaki kayıt beklemeden gönderilebilmeli.
        _, payload, _ = kuyruk.peek()
        self.assertEqual(payload["idempotency_key"], "B")

    def test_eski_surumun_kuyrugu_guncellemede_kaybolmaz(self):
        # Sahadaki Pi'ler bu dosyayı eski şemayla (next_attempt_at sütunu YOK)
        # tutuyor. Güncellemeden sonra içindeki bekleyen kayıt gönderilebilmeli.
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                """
                CREATE TABLE events (
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
                "INSERT INTO events (idempotency_key, payload, created_at) VALUES (?, ?, ?)",
                ("ESKI-1", '{"idempotency_key": "ESKI-1"}', "2026-07-30T10:00:00+00:00"),
            )

        kuyruk = PersistentQueue(self.db)

        oge = kuyruk.peek()
        self.assertIsNotNone(oge, "eski sürümden kalan bekleyen kayıt kayboldu")
        self.assertEqual(oge[1]["idempotency_key"], "ESKI-1")


class EnvTests(unittest.TestCase):
    """Env dosyasında boş bırakılmış satır ajanı çökertmemeli."""

    ANAHTARLAR = (
        "UDAR_POLL_INTERVAL_SECONDS", "UDAR_SEND_RETRY_BASE_SECONDS",
        "UDAR_DURATION_DROPOUT_GRACE_SECONDS", "UDAR_QUEUE_DB", "UDAR_MEASUREMENT_MODE",
        "UDAR_BOUNCE_SECONDS",
    )

    def setUp(self):
        import os
        self._os = os
        self._eski = {k: os.environ.get(k) for k in self.ANAHTARLAR + ("UDAR_CRM_URL", "UDAR_DEVICE_TOKEN")}
        os.environ["UDAR_CRM_URL"] = "https://ornek.test"
        os.environ["UDAR_DEVICE_TOKEN"] = "test"

    def tearDown(self):
        for k, v in self._eski.items():
            if v is None:
                self._os.environ.pop(k, None)
            else:
                self._os.environ[k] = v

    def test_bos_birakilan_ayarlar_ondegere_duser(self):
        from udar_pi_agent import load_config
        for k in self.ANAHTARLAR:
            self._os.environ[k] = ""
        config = load_config()   # eskiden burada float("") ile çöküyordu
        self.assertEqual(config.poll_interval_seconds, 0.002)
        self.assertEqual(config.send_retry_base_seconds, 5.0)
        self.assertEqual(config.duration_dropout_grace_seconds, 1.50)
        self.assertEqual(str(config.queue_db), "/var/lib/udar-pi-agent/machine_events.sqlite3")
        self.assertEqual(config.measurement_mode, "pulse")

    def test_bozuk_sayi_traceback_degil_ne_yapilacagini_soyler(self):
        # 2026-09-21 saha arizasi: tek satirlik kurulumda sihirbaz betigin kendi
        # satirlarini cevap sandi ve bu degeri yazdi; ajan her acilista traceback
        # verip coktu. Artik ayarin adini ve duzeltme komutunu soyluyor.
        from udar_pi_agent import load_config
        self._os.environ["UDAR_BOUNCE_SECONDS"] = 'echo "  sudo systemctl restart udar-pi-agent"'
        try:
            with self.assertRaises(SystemExit) as ctx:
                load_config()
        finally:
            self._os.environ.pop("UDAR_BOUNCE_SECONDS", None)
        mesaj = str(ctx.exception.code)
        self.assertIn("UDAR_BOUNCE_SECONDS", mesaj)
        self.assertIn("install.sh --configure", mesaj)
