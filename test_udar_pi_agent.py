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

from udar_pi_agent import Config, PersistentQueue, retry_delay, transport_payload


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

        oge = kuyruk.peek()
        self.assertEqual(oge.payload["idempotency_key"], "A")

        kuyruk.fail(oge.row_id, "400 geçersiz kayıt", retry_after_seconds=60)

        # A 60 sn ertelendi; sıradaki kayıt beklemeden gönderilebilmeli.
        self.assertEqual(kuyruk.peek().payload["idempotency_key"], "B")

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
        self.assertEqual(oge.payload["idempotency_key"], "ESKI-1")
        # Eski satırda monoton saat ve açılış kimliği yok: kuyruğa giriş anı
        # satırın created_at'inden gelir, bekleme süresi bilinmez (gönderilmez).
        self.assertEqual(oge.queued_at, "2026-07-30T10:00:00+00:00")
        self.assertIsNone(oge.queue_age_seconds)
        govde = transport_payload(oge)
        self.assertEqual(govde["attempts"], 1)
        self.assertEqual(govde["queued_at"], "2026-07-30T10:00:00+00:00")
        self.assertNotIn("queue_age_seconds", govde)
        # Kaydı yeni ajan gönderiyor ama ölçen eski ajandı: ölçen sürüm boş
        # (null) gider, gönderen sürümle karıştırılmaz.
        self.assertIn("measured_by_version", govde)
        self.assertIsNone(govde["measured_by_version"])
        self.assertIn("agent_version", govde)


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
        self._os.environ["UDAR_PULSE_REARM_SECONDS"] = 'echo "  sudo systemctl restart udar-pi-agent"'
        try:
            with self.assertRaises(SystemExit) as ctx:
                load_config()
        finally:
            self._os.environ.pop("UDAR_PULSE_REARM_SECONDS", None)
        mesaj = str(ctx.exception.code)
        self.assertIn("UDAR_PULSE_REARM_SECONDS", mesaj)
        self.assertIn("install.sh --configure", mesaj)

    def test_kullanilmayan_bounce_ayari_ajani_cokertmez(self):
        # UDAR_BOUNCE_SECONDS Temmuz 2026'dan (391b844) beri kullanilmiyor; daha
        # eski ajanda gpiozero Button bounce_time idi. 2026-09-21'deki cop deger
        # tam da bu anahtara yazilmisti; kullanilmayan bir ayar yuzunden ajan
        # acilmamazlik etmemeli.
        from udar_pi_agent import load_config
        self._os.environ["UDAR_BOUNCE_SECONDS"] = 'echo "  sudo systemctl restart udar-pi-agent"'
        config = load_config()
        self.assertFalse(hasattr(config, "bounce_time"))


# ---------------------------------------------------------------------------
# 2026-09-30: sinyal güvenilirliği (tasarım B3)
#
# Bir makinenin sinyal gürültüsü binlerce sahte vuruş üretti. Sunucu tarafı
# koruması ayrı; burada ajanın payı: öndeğerler DEĞİŞMEZ (ürün tarafsız), 'both'
# belgesi koda uyar, aynı Pi'de ikinci ajan çalışamaz, her kayıt sürüm / saat
# durumu / gönderim bilgisi taşır, ayar makine türüne göre değil ölçüme göre
# önerilir.
# ---------------------------------------------------------------------------

import argparse
import bisect
import contextlib
import io
import json
import os
import random
import re
import shutil
import subprocess
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest import mock

import diagnose_gpio
import udar_pi_agent
from udar_pi_agent import (
    AGENT_VERSION,
    DEFAULT_PULSE_MIN_INTERVAL_SECONDS,
    ClockSyncMonitor,
    acquire_single_instance_lock,
    enqueue_measurement,
    load_config,
    lock_path_for,
    min_interval_suggestion,
    pulse_active_level,
    sender_loop,
)

BURASI = Path(__file__).resolve().parent


@contextmanager
def _ortam(**degerler):
    """Yalnız verilen UDAR_* ayarlarıyla çalıştırır; diğer UDAR_* ayarları yok sayılır."""
    temiz = {k: v for k, v in os.environ.items() if not k.startswith("UDAR_")}
    temiz.update({"UDAR_CRM_URL": "https://ornek.test", "UDAR_DEVICE_TOKEN": "test"})
    temiz.update(degerler)
    with mock.patch.dict(os.environ, temiz, clear=True):
        yield


def _env_ornegi():
    """udar-pi-agent.env.example içindeki (yorum olmayan) KEY=VALUE satırları."""
    ayarlar = {}
    for satir in (BURASI / "udar-pi-agent.env.example").read_text(encoding="utf-8").splitlines():
        satir = satir.strip()
        if satir and not satir.startswith("#") and "=" in satir:
            anahtar, deger = satir.split("=", 1)
            ayarlar[anahtar] = deger
    return ayarlar


class OndegerTests(unittest.TestCase):
    """Öndeğerler ürün tarafsızdır ve DEĞİŞMEZ: değişirse sahadaki her kurulumun sayımı değişir."""

    BEKLENEN = {
        "pull_up": False,
        "pulse_edge": "rising",
        "poll_interval_seconds": 0.002,
        "pulse_min_active_seconds": 0.02,
        "pulse_rearm_seconds": 0.20,
        "pulse_max_active_seconds": 10.0,
        "pulse_min_interval_seconds": 0.20,
    }

    def test_ayar_yokken_ondegerler(self):
        with _ortam():
            config = load_config()
        for alan, deger in self.BEKLENEN.items():
            with self.subTest(alan=alan):
                self.assertEqual(getattr(config, alan), deger)
        self.assertEqual(DEFAULT_PULSE_MIN_INTERVAL_SECONDS, 0.20)

    def test_ornek_env_dosyasi_ayni_ondegerleri_yazar(self):
        ornek = _env_ornegi()
        self.assertEqual(ornek["UDAR_PULL_UP"], "false")
        self.assertEqual(ornek["UDAR_PULSE_EDGE"], "rising")
        self.assertEqual(float(ornek["UDAR_PULSE_MIN_ACTIVE_SECONDS"]), 0.02)
        self.assertEqual(float(ornek["UDAR_PULSE_REARM_SECONDS"]), 0.20)
        self.assertEqual(float(ornek["UDAR_PULSE_MAX_ACTIVE_SECONDS"]), 10.0)
        self.assertEqual(float(ornek["UDAR_PULSE_MIN_INTERVAL_SECONDS"]), 0.20)
        # Kullanılmayan ayar yeni kurulumlara yazılmaz.
        self.assertNotIn("UDAR_BOUNCE_SECONDS", ornek)


class KenarBelgesiTests(unittest.TestCase):
    """'both' her zaman rising gibi çalıştı; belge artık bunu söylüyor, kod değişmedi."""

    def test_both_rising_ile_ayni_seviyeyi_aktif_sayar(self):
        self.assertIs(pulse_active_level("rising"), True)
        self.assertIs(pulse_active_level("both"), True)
        self.assertIs(pulse_active_level("falling"), False)

    def test_both_tam_cevrimi_bir_kez_sayar_her_degisimi_degil(self):
        detector = PulseCycleDetector(
            active_level=pulse_active_level("both"),
            min_active_seconds=0.02,
            rearm_seconds=0.20,
            max_active_seconds=2.0,
            min_interval_seconds=0.20,
        )
        sonuclar = [
            detector.feed(deger, an)
            for deger, an in ((False, 0.0), (False, 0.3), (True, 0.31), (True, 0.34), (False, 0.50), (False, 0.71), (False, 1.5))
        ]
        kabul = [s for s in sonuclar if s and s.get("accepted")]
        self.assertEqual(len(kabul), 1, "iki değişimli tek çevrim tek vuruş olmalı")

    def test_eski_both_ayari_hala_kabul_edilir(self):
        with _ortam(UDAR_PULSE_EDGE="both"):
            self.assertEqual(load_config().pulse_edge, "both")

    def test_belgeler_eski_yanlis_aciklamayi_tasimaz(self):
        yanlislar = ("count every state change", "her 0/1 degisimini say", "her değişimi say")
        for dosya in ("udar-pi-agent.env.example", "install.sh", "README.md"):
            metin = (BURASI / dosya).read_text(encoding="utf-8")
            for yanlis in yanlislar:
                with self.subTest(dosya=dosya, yanlis=yanlis):
                    self.assertNotIn(yanlis, metin)

    def test_sihirbaz_both_onermez(self):
        metin = (BURASI / "install.sh").read_text(encoding="utf-8")
        satir = next(s for s in metin.splitlines() if 'ask_choice "Pulse kenari"' in s)
        self.assertNotIn("both", satir)


class CevrimZamaniTests(unittest.TestCase):
    def test_kabul_edilen_cevrim_baslangic_ve_bitisini_verir(self):
        detector = PulseCycleDetector(
            active_level=True, min_active_seconds=0.02, rearm_seconds=0.20,
            max_active_seconds=2.0, min_interval_seconds=0.20,
        )
        for deger, an in ((False, 0.0), (False, 0.3), (True, 0.31), (True, 0.34), (False, 0.50)):
            detector.feed(deger, an)
        sonuc = detector.feed(False, 0.71)
        self.assertTrue(sonuc["accepted"])
        self.assertAlmostEqual(sonuc["started_at"], 0.31)
        self.assertAlmostEqual(sonuc["ended_at"], 0.50)


class SurecKilidiTests(unittest.TestCase):
    """Aynı Pi'de iki ajan aynı makineyi iki kez sayar; ikincisi başlamamalı."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "durum" / "machine_events.sqlite3"

    def tearDown(self):
        self._tmp.cleanup()

    def test_kilit_kuyruk_dosyasinin_yaninda(self):
        kilit = acquire_single_instance_lock(self.db)
        self.addCleanup(kilit.close)
        self.assertEqual(lock_path_for(self.db).parent, self.db.parent)
        self.assertTrue(lock_path_for(self.db).exists())
        self.assertEqual(lock_path_for(self.db).read_text().strip(), str(os.getpid()))

    def test_ikinci_kopya_nedenini_yazip_cikar(self):
        kilit = acquire_single_instance_lock(self.db)
        self.addCleanup(kilit.close)
        with self.assertRaises(SystemExit) as ctx:
            acquire_single_instance_lock(self.db)
        mesaj = str(ctx.exception.code)
        self.assertIn("AJAN ZATEN CALISIYOR", mesaj)
        self.assertIn(str(os.getpid()), mesaj)
        self.assertIn("pgrep -af udar_pi_agent", mesaj)

    def test_ajan_durunca_kilit_kendiliginden_birakilir(self):
        acquire_single_instance_lock(self.db).close()
        acquire_single_instance_lock(self.db).close()  # dosya kaldı ama kilit yok

    def test_baska_surecteki_ajan_engeller_ve_olunce_birakir(self):
        # Gerçek durum: iki ayrı süreç. Çocuk süreç kilidi alır ve bekler.
        cocuk_kodu = (
            "import sys, types\n"
            "for ad, oz in (('requests', {'RequestException': Exception, 'Session': object}),"
            " ('gpiozero', {'DigitalInputDevice': object})):\n"
            "    m = types.ModuleType(ad); m.__dict__.update(oz); sys.modules.setdefault(ad, m)\n"
            f"sys.path.insert(0, {str(BURASI)!r})\n"
            "from pathlib import Path\n"
            "import udar_pi_agent\n"
            "kilit = udar_pi_agent.acquire_single_instance_lock(Path(sys.argv[1]))\n"
            "print('hazir', flush=True)\n"
            "sys.stdin.read()\n"
        )
        cocuk = subprocess.Popen(
            [sys.executable, "-c", cocuk_kodu, str(self.db)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        try:
            self.assertEqual(cocuk.stdout.readline().strip(), "hazir")
            with self.assertRaises(SystemExit) as ctx:
                acquire_single_instance_lock(self.db)
            self.assertIn(f"pid {cocuk.pid}", str(ctx.exception.code))
        finally:
            cocuk.stdin.close()
            cocuk.wait(timeout=10)
            cocuk.stdout.close()
        acquire_single_instance_lock(self.db).close()


class SaatTests(unittest.TestCase):
    """Pi'de pilli saat yok; kayıt, ölçüm anında saatin eşitlenip eşitlenmediğini taşır."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.isaret = Path(self._tmp.name) / "synchronized"

    def tearDown(self):
        self._tmp.cleanup()

    def _izleyici(self, cevap=None, komut=None):
        if komut is None:
            komut = (sys.executable, "-c", f"print({cevap!r})")
        return ClockSyncMonitor(marker=self.isaret, command=komut, interval_seconds=0.01)

    def test_timesyncd_isaret_dosyasi_varsa_eslenmis(self):
        self.isaret.touch()
        self.assertIs(self._izleyici(komut=("/yok/timedatectl",)).check(), True)

    def test_timedatectl_hayir_derse_eslenmemis(self):
        self.assertIs(self._izleyici("no").check(), False)

    def test_timedatectl_evet_derse_eslenmis(self):
        self.assertIs(self._izleyici("yes").check(), True)

    def test_sorulamazsa_bilinmiyor(self):
        self.assertIsNone(self._izleyici(komut=("/yok/timedatectl",)).check())
        self.assertIsNone(self._izleyici("anlamsiz").check())

    def test_bir_kez_eslenince_yapiskan(self):
        izleyici = self._izleyici("yes")
        self.assertIs(izleyici.check(), True)
        izleyici.command = (sys.executable, "-c", "print('no')")
        self.assertIs(izleyici.check(), True)

    def test_izleme_eslenince_durur(self):
        izleyici = self._izleyici("no")
        dur = threading.Event()
        is_parcacigi = threading.Thread(target=izleyici.run, args=(dur,), daemon=True)
        is_parcacigi.start()
        self.isaret.touch()
        is_parcacigi.join(timeout=5)
        self.assertFalse(is_parcacigi.is_alive())
        self.assertIs(izleyici.synced, True)
        dur.set()


class GonderimAlanlariTests(unittest.TestCase):
    """agent_version, sent_at, queued_at, attempts, clock_synced, queue_age_seconds."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "kuyruk.sqlite3"
        self.saat = [100.0]

    def tearDown(self):
        self._tmp.cleanup()

    def _kuyruk(self, acilis="acilis-1"):
        return PersistentQueue(self.db, boot_id=acilis, monotonic=lambda: self.saat[0])

    def test_gonderimde_surum_zaman_ve_deneme_eklenir(self):
        kuyruk = self._kuyruk()
        kuyruk.put({"idempotency_key": "K1", "timestamp": "2026-09-30T08:59:52+00:00"})
        self.saat[0] = 107.5
        oge = kuyruk.peek()
        simdi = datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)
        govde = transport_payload(oge, now=simdi)

        self.assertEqual(govde["agent_version"], AGENT_VERSION)
        self.assertEqual(govde["sent_at"], simdi.isoformat())
        self.assertEqual(govde["attempts"], 1)
        self.assertEqual(govde["queued_at"], oge.queued_at)
        datetime.fromisoformat(govde["queued_at"])  # ISO biçiminde
        self.assertEqual(govde["queue_age_seconds"], 7.5)
        self.assertEqual(govde["idempotency_key"], "K1")
        self.assertEqual(govde["timestamp"], "2026-09-30T08:59:52+00:00")
        # Kuyruktaki ölçüm değişmez; gönderim bilgisi her denemede yeniden hesaplanır.
        self.assertNotIn("sent_at", kuyruk.peek().payload)

    def test_sunucunun_olay_zamani_anahtarlariyla_cakismaz(self):
        # Sunucu olay zamanını occurred_at > timestamp > time > created_at
        # sırasıyla okur (_machine_event_timestamp). Yeni alanlar bu adları
        # KULLANMAMALI; yoksa ölçüm anı yerine gönderim anı olay zamanı olurdu.
        kuyruk = self._kuyruk()
        enqueue_measurement(_config(), kuyruk, delta=1.0, clock_synced=True)
        govde = transport_payload(kuyruk.peek())
        self.assertFalse({"occurred_at", "time", "created_at"} & set(govde))

    def test_basarisiz_denemeden_sonra_attempts_artar(self):
        kuyruk = self._kuyruk()
        kuyruk.put({"idempotency_key": "K1"})
        kuyruk.fail(kuyruk.peek().row_id, "HTTP 500", retry_after_seconds=1)
        with sqlite3.connect(self.db) as conn:  # beklemeyi atla
            conn.execute("UPDATE events SET next_attempt_at = ''")
        self.assertEqual(transport_payload(kuyruk.peek())["attempts"], 2)

    def test_pi_yeniden_basladiysa_bekleme_suresi_gonderilmez(self):
        self._kuyruk("acilis-1").put({"idempotency_key": "K1"})
        self.saat[0] = 5.0  # yeni açılışta monoton saat baştan başlar
        oge = self._kuyruk("acilis-2").peek()
        self.assertIsNone(oge.queue_age_seconds)
        self.assertNotIn("queue_age_seconds", transport_payload(oge))

    def test_olcen_surum_olcum_aninda_kuyruga_yazilir(self):
        kuyruk = self._kuyruk()
        enqueue_measurement(_config(), kuyruk, delta=1.0)
        self.assertEqual(kuyruk.peek().payload["measured_by_version"], AGENT_VERSION)

    def test_gonderen_surum_olcen_surumden_ayri_kalir(self):
        # Pi internetsizken başka bir sürümün kuyruğa yazdığı ölçüm, güncellemeden
        # sonra bu ajanla gider: ölçen sürüm kayıttaki kalır, gönderen bu ajandır.
        kuyruk = self._kuyruk()
        kuyruk.put({"idempotency_key": "K1", "measured_by_version": "BASKA-SURUM"})
        govde = transport_payload(kuyruk.peek())
        self.assertEqual(govde["measured_by_version"], "BASKA-SURUM")
        self.assertEqual(govde["agent_version"], AGENT_VERSION)

    def test_olcum_kaydi_saat_durumunu_tasir(self):
        kuyruk = self._kuyruk()
        enqueue_measurement(_config(), kuyruk, delta=1.0, clock_synced=False)
        self.assertIs(kuyruk.peek().payload["clock_synced"], False)

    def test_saat_durumu_bilinmiyorsa_null_gider(self):
        kuyruk = self._kuyruk()
        enqueue_measurement(_config(), kuyruk, delta=1.0)
        payload = kuyruk.peek().payload
        self.assertIn("clock_synced", payload)
        self.assertIsNone(payload["clock_synced"])

    def test_gonderici_alanlari_yollar_ve_yerel_loga_yazar(self):
        kuyruk = self._kuyruk()
        enqueue_measurement(_config(), kuyruk, delta=1.0, clock_synced=True)
        dur = threading.Event()
        gonderilen = []

        class SahteOturum:
            def post(self, url, json, timeout):  # noqa: A002 (requests imzası)
                gonderilen.append(json)
                dur.set()
                return types.SimpleNamespace(status_code=201, text="{}", headers={})

        with mock.patch.object(udar_pi_agent.requests, "Session", SahteOturum):
            sender_loop(_config(), kuyruk, dur)

        self.assertEqual(len(gonderilen), 1)
        govde = gonderilen[0]
        for alan in ("agent_version", "measured_by_version", "sent_at", "queued_at", "attempts",
                     "clock_synced", "queue_age_seconds"):
            self.assertIn(alan, govde)
        self.assertEqual(govde["measured_by_version"], AGENT_VERSION)
        self.assertEqual(govde["attempts"], 1)
        self.assertIs(govde["clock_synced"], True)
        self.assertIsNone(kuyruk.peek(), "gönderilen kayıt kuyrukta kaldı")
        with sqlite3.connect(self.db) as conn:
            durum, yuk = conn.execute("SELECT status, payload FROM event_log ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(durum, "sent")
        self.assertEqual(json.loads(yuk)["attempts"], 1)


class AralikOnerisiTests(unittest.TestCase):
    """install.sh'nin 'en kısa gerçek çevrim' sorusu ve diagnose_gpio.py aynı kuralı kullanır."""

    ORNEKLER = {0: 0.20, 3.0: 1.5, 1.7: 0.85, 0.5: 0.25, 0.3: 0.20, 0.26: 0.20, 0.25: 0.12, 0.2: 0.1}

    def test_kural(self):
        for cevrim, beklenen in self.ORNEKLER.items():
            with self.subTest(cevrim=cevrim):
                self.assertAlmostEqual(min_interval_suggestion(cevrim), beklenen)

    def test_oneri_gercek_cevrimi_asla_asmaz(self):
        for yuzde in range(1, 1000):
            cevrim = yuzde / 100
            with self.subTest(cevrim=cevrim):
                self.assertLess(min_interval_suggestion(cevrim), cevrim)

    def test_install_sh_ayni_kurali_uygular(self):
        awk = shutil.which("awk")
        if not awk:
            self.skipTest("awk yok")
        metin = (BURASI / "install.sh").read_text(encoding="utf-8")
        eslesme = re.search(r"^ARALIK_ONERISI_AWK='(.*)'$", metin, re.MULTILINE)
        self.assertIsNotNone(eslesme, "install.sh içinde ARALIK_ONERISI_AWK bulunamadı")
        for cevrim in list(self.ORNEKLER)[1:] + [0.29, 0.45, 1.23, 2.5, 7.77, 12.0]:
            with self.subTest(cevrim=cevrim):
                cikti = subprocess.run(
                    [awk, "-v", f"c={cevrim}", eslesme.group(1)],
                    capture_output=True, text=True, check=True,
                ).stdout.strip()
                self.assertAlmostEqual(float(cikti), min_interval_suggestion(cevrim))


def _cevrimler(adet, *, aralik, aktif, bas=10.0):
    return [(bas + i * aralik, bas + i * aralik + aktif) for i in range(adet)]


# Ajanın öndeğerleri, diagnose_gpio'nun ayar sözlüğü biçiminde.
ONDEGER_AYAR = {"edge": "rising", "min_active": 0.02, "rearm": 0.20, "max_active": 10.0, "min_interval": 0.20}


def _kayit(sinyal, bitis, *, adim=0.002):
    """Sinyali ajan gibi ``adim`` aralıkla okur; yeniden oynatma kaydını döndürür."""
    kayit = diagnose_gpio.OrnekKaydi()
    for sira in range(int(round(bitis / adim)) + 1):
        an = sira * adim
        kayit.ekle(an, bool(sinyal(an)))
    return kayit.bitir()


def _cozumle(ornekler, ayar=ONDEGER_AYAR):
    """(özet, mevcut ayarla sayım, önerilen ayarla sayım) — diagnose_gpio.main'in yaptığı gibi."""
    ozet = diagnose_gpio.olcum_ozeti(diagnose_gpio.olcum_parcalari(ornekler, True))
    mevcut = diagnose_gpio.ajan_sayimi(ornekler, ayar)
    onerilen = None
    if ozet["oneri"]:
        onerilen = diagnose_gpio.ajan_sayimi(ornekler, diagnose_gpio.oneri_ayari(ayar, ozet["oneri"]))
    return ozet, mevcut, onerilen


def _ozet_metni(ozet, mevcut, onerilen, *, ayar=ONDEGER_AYAR, is_sayisi=None):
    return "\n".join(diagnose_gpio.ozet_satirlari(
        ozet, sure=60, ham_sayi=0, ajan_sayisi=mevcut, onerilen_sayi=onerilen, ayar=ayar, is_sayisi=is_sayisi,
    ))


def _acik_ayar_satirlari(metin):
    """Ayar dosyasına yapıştırılınca etkisi olacak (yorum olmayan) satırlar."""
    return [satir for satir in metin.splitlines() if satir.startswith("UDAR_PULSE_")]


def _kopmali_sinyal(an):
    # 3 sn'de bir gerçek iş: 0,30 sn aktif, ortasında 16 ms kopma (röle / limit
    # şalteri sıçraması). 1-62 sn arasında 21 iş.
    if an < 1.0:
        return False
    faz = (an - 1.0) % 3.0
    return faz < 0.14 or 0.156 <= faz < 0.30


PARAZITLER = (2.0, 7.3, 13.1, 19.9, 26.4)


def _parazitli_sinyal(an):
    # Önce makine kapalı: 8 ms'lik 5 parazit. 30. sn'den sonra 3 sn'de bir 0,30
    # sn'lik gerçek iş; 30-89 sn arasında 20 iş.
    if any(bas <= an < bas + 0.008 for bas in PARAZITLER):
        return True
    return an >= 30.0 and (an - 30.0) % 3.0 < 0.30


def _cift_vurus_sinyali(an):
    # Her 6 sn'de İKİ gerçek vuruş: 0,30 sn aktif, 0,30 sn boş, 0,30 sn aktif.
    # 1-91 sn arasında 30 iş. Aradaki boşluk kısa ama sinyal kadar uzun: kopma değil.
    if an < 1.0:
        return False
    faz = (an - 1.0) % 6.0
    return faz < 0.3 or 0.6 <= faz < 0.9


def _hizli_sinyal(an):
    # 0,2 sn'de bir 0,05 sn aktif: öndeğer süzgeci (REARM 0,20) hiçbirini saymaz.
    return an > 0.5 and (an % 0.2) < 0.05


def _olaylardan(olaylar):
    """(başı, sonu) aktif aralıklarından sinyal; çok olayda da hızlı."""
    olaylar = sorted(olaylar)
    baslar = [bas for bas, _ in olaylar]

    def sinyal(an):
        sira = bisect.bisect_right(baslar, an) - 1
        return sira >= 0 and an < olaylar[sira][1]

    return sinyal


def _partili(aktif, bos, parti, parti_sayisi, duraklama, bas=1.0):
    """Partiler hâlinde çalışan makine: partide ``parti`` iş (aktif + boş), partiler
    arasında ``duraklama`` (sac yükleme). Döner: (sinyal, bitiş, gerçek iş)."""
    olaylar, an = [], bas
    for _ in range(parti_sayisi):
        for _ in range(parti):
            olaylar.append((an, an + aktif))
            an += aktif + bos
        an += duraklama
    return _olaylardan(olaylar), an + 1.0, parti * parti_sayisi


def _kisa_darbe(uzun_basislar, adet=25):
    """Kısa darbeli sensör: 2 sn'de bir 0,05 sn; ``uzun_basislar`` sıradaki işlerde
    operatör pedalı 0,6 sn tutmuş. Döner: (sinyal, bitiş, gerçek iş)."""
    olaylar = [(1.0 + 2.0 * i, 1.0 + 2.0 * i + (0.6 if i in uzun_basislar else 0.05)) for i in range(adet)]
    return _olaylardan(olaylar), 2.0 * adet + 2.0, adet


class OlcumOnerisiTests(unittest.TestCase):
    """diagnose_gpio.py: ayar makine türüne göre değil, ölçülen sinyale göre önerilir."""

    def test_az_cevrimde_oneri_yok(self):
        ozet = diagnose_gpio.olcum_ozeti(_cevrimler(diagnose_gpio.EN_AZ_CEVRIM - 1, aralik=3.0, aktif=0.3))
        self.assertEqual(ozet["cevrim"], diagnose_gpio.EN_AZ_CEVRIM - 1)
        self.assertIsNone(ozet["oneri"])
        metin = "\n".join(diagnose_gpio.ozet_satirlari(
            ozet, sure=30, ham_sayi=9, ajan_sayisi=9, onerilen_sayi=None, ayar=ONDEGER_AYAR,
        ))
        self.assertIn(f"en az {diagnose_gpio.EN_AZ_CEVRIM}", metin)
        self.assertNotIn("ONERILEN AYARLAR", metin)

    def test_oneri_olcumden_hesaplanir(self):
        ozet = diagnose_gpio.olcum_ozeti(_cevrimler(20, aralik=3.0, aktif=0.3))
        self.assertAlmostEqual(ozet["aralik"][0], 3.0)
        self.assertFalse(ozet["hizli"])
        oneri = ozet["oneri"]
        self.assertAlmostEqual(oneri["UDAR_PULSE_MIN_ACTIVE_SECONDS"], 0.15)
        self.assertAlmostEqual(oneri["UDAR_PULSE_REARM_SECONDS"], 0.50)  # 2,7/2 -> en çok 0,50
        self.assertAlmostEqual(oneri["UDAR_PULSE_MAX_ACTIVE_SECONDS"], 10.0)
        self.assertAlmostEqual(oneri["UDAR_PULSE_MIN_INTERVAL_SECONDS"], 1.5)

    def test_hizli_makinede_oneri_cevrimi_asmaz_ve_uyarir(self):
        ozet = diagnose_gpio.olcum_ozeti(_cevrimler(20, aralik=0.2, aktif=0.05))
        self.assertTrue(ozet["hizli"])
        self.assertLess(ozet["oneri"]["UDAR_PULSE_MIN_INTERVAL_SECONDS"], 0.2)
        self.assertLess(ozet["oneri"]["UDAR_PULSE_REARM_SECONDS"], 0.15)

    def test_olcum_suzgeci_sicramayi_cevrim_saymaz(self):
        # 1 ms örneklemeli yapay sinyal: 3 sn'de bir 0,3 sn aktif; her aktifin
        # başında 2 ms'lik kontak sıçraması. Ölçüm 12 gerçek çevrim görmeli.
        def sinyal(an):
            faz = (an - 1.0) % 3.0 if an >= 1.0 else None
            return faz is not None and (faz < 0.3) and not (0.001 <= faz < 0.003)

        parcalar = diagnose_gpio.olcum_parcalari(_kayit(sinyal, 37.0, adim=0.001), True)
        self.assertEqual(len(parcalar), 12)
        ozet = diagnose_gpio.olcum_ozeti(parcalar)
        self.assertEqual(ozet["cevrim"], 12)
        self.assertAlmostEqual(ozet["aralik"][0], 3.0, places=2)

    def test_ayar_dosyasi_okunur(self):
        with tempfile.TemporaryDirectory() as klasor:
            yol = Path(klasor) / "udar-pi-agent.env"
            yol.write_text('# yorum\n\nUDAR_GPIO_BCM=17\nUDAR_NOTE="GPIO17 deneme"\nUDAR_LINE_ID=\n', encoding="utf-8")
            self.assertEqual(
                diagnose_gpio.ayar_dosyasini_oku(yol),
                {"UDAR_GPIO_BCM": "17", "UDAR_NOTE": "GPIO17 deneme", "UDAR_LINE_ID": ""},
            )
            self.assertIsNone(diagnose_gpio.ayar_dosyasini_oku(Path(klasor) / "yok.env"))

    def test_komut_satiri_dosyayi_ezer_dosya_ondegeri(self):
        args = argparse.Namespace(pin=None, pull_up=None, edge=None, min_active=0.1, rearm=None,
                                  max_active=None, min_interval=None)
        dosya = {"UDAR_GPIO_BCM": "17", "UDAR_PULL_UP": "true", "UDAR_PULSE_EDGE": "falling",
                 "UDAR_PULSE_MIN_ACTIVE_SECONDS": "0.05", "UDAR_PULSE_REARM_SECONDS": "0.5"}
        ayar = diagnose_gpio.ayarlari_belirle(args, dosya)
        self.assertEqual(
            ayar,
            {"pin": 17, "pull_up": True, "edge": "falling", "min_active": 0.1, "rearm": 0.5,
             "max_active": 10.0, "min_interval": 0.20},
        )
        # Dosya okunamadıysa ajanın öndeğerleri (pin: fiziksel 13 = GPIO27).
        bos = argparse.Namespace(pin=None, pull_up=None, edge=None, min_active=None, rearm=None,
                                 max_active=None, min_interval=None)
        self.assertEqual(
            diagnose_gpio.ayarlari_belirle(bos, None),
            {"pin": 27, "pull_up": False, "edge": "rising", "min_active": 0.02, "rearm": 0.20,
             "max_active": 10.0, "min_interval": 0.20},
        )


class SaglamOneriTests(unittest.TestCase):
    """diagnose_gpio.py önerisi tek aykırı değere değil sağlam istatistiğe dayanır.

    Faz A-3 doğrulayıcı bulgusu: eski öneri en kısa değerlere bakıyordu. Çevrimin
    ortasındaki 16 ms'lik kopma REARM'ı 0,01'e, makine kapalıyken gelen 8 ms'lik
    parazit MIN_ACTIVE'i 0,005'e indiriyordu; "aynen yapıştır" denen bu ayarlarla
    ajan her işi iki kez ya da parazitleri de sayıyordu. Doğru sayan ajan da
    "ölçümden farklı" diye uyarılıyordu.
    """

    def test_cevrim_icindeki_kisa_kopma_cift_vurus_yaptirmaz(self):
        ornekler = _kayit(_kopmali_sinyal, 62.0)
        self.assertEqual(len(diagnose_gpio.olcum_parcalari(ornekler, True)), 42)  # ham parçalar
        ozet, mevcut, onerilen = _cozumle(ornekler)
        self.assertEqual(ozet["cevrim"], 21)
        self.assertEqual(ozet["kopma"][0], 21)
        self.assertEqual(mevcut, 21)
        self.assertEqual(onerilen, 21, "önerilen ayarla ajan işleri iki kez saymamalı")
        self.assertEqual(diagnose_gpio.gevseten_oneriler(ozet["oneri"], ONDEGER_AYAR), {})
        self.assertAlmostEqual(ozet["oneri"]["UDAR_PULSE_REARM_SECONDS"], 0.50)
        self.assertAlmostEqual(ozet["oneri"]["UDAR_PULSE_MIN_INTERVAL_SECONDS"], 1.5)
        # Ajan aktifliğin KESİNTİSİZ sürmesini ister: öneri toplam 0,30'dan değil
        # en uzun kesintisiz parçadan (0,144) gelir; yoksa ajan hiçbirini saymazdı.
        self.assertLess(ozet["oneri"]["UDAR_PULSE_MIN_ACTIVE_SECONDS"], 0.14)
        metin = _ozet_metni(ozet, mevcut, onerilen)
        self.assertNotIn("farkli", metin)
        self.assertIn("olcumle ayni (21)", metin)
        # İş sayısı yokken "aynen yapıştır" denmez, kontrol için iş sayısı istenir.
        self.assertNotIn("AYNEN", metin)
        self.assertIn("Kontrol icin is sayisini girin", metin)
        self.assertEqual(_acik_ayar_satirlari(metin), [])
        metin = _ozet_metni(ozet, mevcut, onerilen, is_sayisi=21)
        self.assertIn("AYNEN", metin)
        self.assertEqual(len(_acik_ayar_satirlari(metin)), 4)

    def test_makine_kapaliyken_gelen_parazit_oneriyi_gevsetmez(self):
        ozet, mevcut, onerilen = _cozumle(_kayit(_parazitli_sinyal, 89.0))
        self.assertEqual(ozet["cevrim"], 20)
        self.assertEqual(ozet["parazit"][0], 5)
        self.assertEqual((mevcut, onerilen), (20, 20))
        self.assertGreaterEqual(ozet["oneri"]["UDAR_PULSE_MIN_ACTIVE_SECONDS"], 0.02)
        self.assertEqual(diagnose_gpio.gevseten_oneriler(ozet["oneri"], ONDEGER_AYAR), {})
        self.assertNotIn("farkli", _ozet_metni(ozet, mevcut, onerilen))

    def test_tek_aykiri_aralik_oneriyi_belirlemez(self):
        # 20 düzenli iş (3 sn) ve bir çift basma: tek bir 0,9 sn'lik aralık.
        parcalar = sorted(_cevrimler(20, aralik=3.0, aktif=0.3) + [(25.9, 26.2)])
        ozet = diagnose_gpio.olcum_ozeti(parcalar)
        self.assertEqual(ozet["cevrim"], 21)
        self.assertAlmostEqual(ozet["oneri"]["UDAR_PULSE_MIN_INTERVAL_SECONDS"], 1.5)
        self.assertAlmostEqual(ozet["oneri"]["UDAR_PULSE_REARM_SECONDS"], 0.50)

    def test_alt_yuzdelik_en_kucugu_hic_tek_basina_kullanmaz(self):
        self.assertEqual(diagnose_gpio.alt_yuzdelik([0.01] + [3.0] * 9), 3.0)
        self.assertEqual(diagnose_gpio.alt_yuzdelik([0.01, 0.02] + [3.0] * 18), 3.0)
        self.assertEqual(diagnose_gpio.alt_yuzdelik([0.5]), 0.5)

    def test_suregen_dagilimda_hicbir_sey_ayiklanmaz(self):
        # Operatör temposu: süreler sürekli dağılır, kısa küme yoktur.
        self.assertIsNone(diagnose_gpio.kisa_kume_esigi([2.1, 2.5, 3.0, 4.2, 6.0, 9.5, 14.0, 30.0], 0.5))
        self.assertIsNone(diagnose_gpio.kisa_kume_esigi([0.2, 0.25, 0.3, 0.45, 0.6, 0.8, 1.0], 0.1))
        # Belirgin uçurum ama kısa taraf üst sınırın üstünde: ayıklanmaz.
        self.assertIsNone(diagnose_gpio.kisa_kume_esigi([0.6, 0.6, 5.0, 5.0], 0.5))
        self.assertAlmostEqual(diagnose_gpio.kisa_kume_esigi([0.016, 0.02] + [2.7] * 5, 0.5), 0.02)
        # Uzun tarafta KUME_EN_AZ'dan az değer: bir-iki uzun değer ayrımı belirleyemez.
        self.assertIsNone(diagnose_gpio.kisa_kume_esigi([0.016, 0.02, 2.7, 2.7], 0.5))

    def test_bosluk_sinyal_kadar_uzunsa_kopma_sayilmaz(self):
        # 0,3 sn arayla iki GERÇEK vuruş: boşluk kısa kümede ama kestiği sinyal
        # (0,3 sn) kadar uzun. Kopma, kestiği sinyalden en az KUME_ORANI kat kısadır.
        ozet, mevcut, onerilen = _cozumle(_kayit(_cift_vurus_sinyali, 91.0))
        self.assertEqual((ozet["cevrim"], ozet["kopma"][0], mevcut, onerilen), (30, 0, 30, 30))

    def test_is_sayisi_olmadan_hicbir_oneri_yapistirilmaz(self):
        # Belirsiz makine: 0,5 sn aktif, 0,1 sn boş, 0,5 sn aktif, 6 sn'de bir; her
        # vuruş gerçek iş (30). Sinyal boşluğun 5 katı: kopmadan ayırt edilemez.
        # Ölçüm de ajan da öneri de 15 der; aynı sayıyı bulmaları doğru saydıklarını
        # göstermez. Yalnız gerçek iş sayısı ayırır.
        olaylar = []
        for sira in range(15):
            bas = 1.0 + 6.0 * sira
            olaylar += [(bas, bas + 0.5), (bas + 0.6, bas + 1.1)]
        ozet, mevcut, onerilen = _cozumle(_kayit(_olaylardan(olaylar), 92.0))
        self.assertEqual((ozet["cevrim"], mevcut, onerilen), (15, 15, 15))
        metin = _ozet_metni(ozet, mevcut, onerilen)
        self.assertIn("olcumle ayni (15)", metin)
        self.assertIn("dogru saydigi bilinmiyor", metin)
        self.assertIn("Kontrol icin is sayisini girin", metin)
        self.assertIn("DOGRUDAN GIRMEYIN", metin)
        self.assertNotIn("AYNEN", metin)
        self.assertEqual(_acik_ayar_satirlari(metin), [])
        metin = _ozet_metni(ozet, mevcut, onerilen, is_sayisi=30)
        self.assertIn("Ajan MEVCUT ayarla 15 sayiyor: 15 EKSIK", metin)
        self.assertIn("ayiklanan kisa kopma/parazit bu makinede gercek is olabilir", metin)
        self.assertEqual(_acik_ayar_satirlari(metin), [])

    def test_gevseten_oneri_ancak_gercek_is_sayisiyla_yapistirilir(self):
        ozet, mevcut, onerilen = _cozumle(_kayit(_hizli_sinyal, 6.0))
        self.assertEqual(mevcut, 0)
        self.assertEqual(onerilen, ozet["cevrim"])
        gevsek = diagnose_gpio.gevseten_oneriler(ozet["oneri"], ONDEGER_AYAR)
        self.assertIn("UDAR_PULSE_REARM_SECONDS", gevsek)
        metin = _ozet_metni(ozet, mevcut, onerilen)
        self.assertIn("DOGRUDAN GIRMEYIN", metin)
        self.assertIn("GEVSETIR", metin)
        self.assertEqual(_acik_ayar_satirlari(metin), [])
        # Gerçek iş sayısı ajanın işleri kaçırdığını ve önerinin doğru saydığını gösterirse girilir.
        metin = _ozet_metni(ozet, mevcut, onerilen, is_sayisi=ozet["cevrim"])
        self.assertIn("AYNEN", metin)
        self.assertIn("UDAR_PULSE_REARM_SECONDS=0.07", _acik_ayar_satirlari(metin))

    def test_ajan_farkli_sayinca_hatali_ilan_edilmez(self):
        ozet = diagnose_gpio.olcum_ozeti(_cevrimler(20, aralik=3.0, aktif=0.3))
        metin = _ozet_metni(ozet, 23, 20)
        self.assertIn("farkli", metin)
        self.assertIn("--is-sayisi", metin)
        self.assertNotIn("hatali", metin.lower())
        self.assertNotIn("UYARI: ajan", metin)
        # Gerçek iş sayısı verilince karar kesin.
        self.assertIn("Ajan MEVCUT ayarla 23 sayiyor: 3 FAZLA", _ozet_metni(ozet, 23, 20, is_sayisi=20))
        self.assertIn("Ajan MEVCUT ayarla DOGRU sayiyor (20)", _ozet_metni(ozet, 20, 20, is_sayisi=20))

    def test_makine_kapali_denemesinde_oneri_yok(self):
        rastgele = random.Random(1)
        durum = {"deger": False}

        def yuzen_giris(_an):
            if rastgele.random() < 0.01:
                durum["deger"] = not durum["deger"]
            return durum["deger"]

        ozet, mevcut, onerilen = _cozumle(_kayit(yuzen_giris, 120.0))
        self.assertGreater(mevcut, 0)
        metin = _ozet_metni(ozet, mevcut, onerilen, is_sayisi=0)
        self.assertIn(f"{mevcut} FAZLA", metin)
        self.assertIn("ayar onerilmez", metin)
        self.assertNotIn("ONERILEN", metin)
        self.assertIn("Kablolama kontrol listesi", metin)

    def test_kayit_butun_orneklerle_ayni_sonucu_verir(self):
        # Yeniden oynatma kaydı yalnız değişimleri ve hemen öncesini tutar; süzgeç
        # sonucu bütün örneklerle birebir aynı olmalı (önerilen ayar sayımı buna dayanır).
        rastgele = random.Random(3)
        durum = {"deger": False}
        tum = []
        for sira in range(60000):
            if rastgele.random() < 0.004:
                durum["deger"] = not durum["deger"]
            tum.append((sira * 0.002, durum["deger"]))
        kayit = diagnose_gpio.OrnekKaydi()
        for an, deger in tum:
            kayit.ekle(an, deger)
        ozet_kayit = kayit.bitir()
        self.assertLess(len(ozet_kayit), len(tum) // 10)
        self.assertEqual(diagnose_gpio.olcum_parcalari(ozet_kayit, True), diagnose_gpio.olcum_parcalari(tum, True))
        for ayar in (ONDEGER_AYAR, dict(ONDEGER_AYAR, rearm=0.05, min_active=0.01, min_interval=0.1),
                     dict(ONDEGER_AYAR, rearm=0.5, min_active=0.15, max_active=0.4, min_interval=1.5)):
            with self.subTest(ayar=ayar):
                self.assertEqual(diagnose_gpio.ajan_sayimi(ozet_kayit, ayar), diagnose_gpio.ajan_sayimi(tum, ayar))

    def test_kayit_siniri_asilinca_durur_ve_oneri_verilmez(self):
        kayit = diagnose_gpio.OrnekKaydi(sinir=10)
        for sira in range(100):
            kayit.ekle(sira * 0.002, sira % 2 == 0)
        self.assertTrue(kayit.kesildi)
        self.assertLessEqual(len(kayit.bitir()), 10)
        ozet = diagnose_gpio.olcum_ozeti(_cevrimler(20, aralik=3.0, aktif=0.3))
        metin = "\n".join(diagnose_gpio.ozet_satirlari(
            ozet, sure=60, ham_sayi=50, ajan_sayisi=5, onerilen_sayi=None, ayar=ONDEGER_AYAR, kesildi=True,
        ))
        self.assertIn("kablolama sorunu", metin)
        self.assertNotIn("ONERILEN", metin)

    def test_uctan_uca_sahte_pinle_dogru_sayan_ajan_uyarilmaz(self):
        # Doğrulayıcının deneyi, main() ile: sahte pin ve sanal saat.
        saat = [0.0]
        bitis = 62.0

        class SahtePin:
            def __init__(self, *args, **kwargs):
                pass

            @property
            def value(self):
                return int(_kopmali_sinyal(saat[0]))

        class SahteZaman:
            @staticmethod
            def monotonic():
                return saat[0]

            @staticmethod
            def sleep(sure):
                saat[0] = round(saat[0] + sure, 6)
                if saat[0] > bitis:
                    raise KeyboardInterrupt

        argv = ["diagnose_gpio.py", "--ayar-dosyasi", "/yok/udar-pi-agent.env", "--is-sayisi", "21"]
        cikti = io.StringIO()
        with mock.patch.object(diagnose_gpio, "DigitalInputDevice", SahtePin), \
                mock.patch.object(diagnose_gpio, "time", SahteZaman), \
                mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(cikti):
            self.assertEqual(diagnose_gpio.main(), 0)
        metin = cikti.getvalue()
        ozet = metin[metin.index("--- OZET"):]
        self.assertIn("Olculen gercek cevrim: 21", ozet)
        self.assertIn("Ajan MEVCUT ayarla DOGRU sayiyor (21)", ozet)
        self.assertIn("Ajan ONERILEN ayarla DOGRU sayiyor (21)", ozet)
        self.assertIn("AYNEN", ozet)
        self.assertNotIn("farkli", ozet)
        self.assertEqual(metin.count("AJAN SAYARDI"), 21)
        # Önerilen satırlar ajanın kendi ayar okuyucusuyla okunabilmeli.
        with _ortam(**dict(satir.split("=", 1) for satir in _acik_ayar_satirlari(ozet))):
            config = load_config()
        self.assertAlmostEqual(config.pulse_rearm_seconds, 0.5)


class DagilimAyiklamaTests(unittest.TestCase):
    """Kopma ve parazit ayıklaması tek bir aykırı değere değil DAĞILIMA dayanır.

    FixA3-pi doğrulayıcı bulgusu: eski kural sıralı değerlerde yalnız yan yana iki
    değerin oranına bakıyordu. Hızlı makinede tek bir duraklama bütün gerçek işleri
    "kopma" diye birleştiriyor, kısa darbeli sensörde tek bir uzun basış bütün
    gerçek darbeleri "parazit" yapıyordu. İş sayısı girilmediğinde de eksik sayan
    ajan onaylanıp "AYNEN yapıştırılabilir" deniyordu.
    """

    def test_tek_duraklama_hizli_makinenin_islerini_kopma_yapmaz(self):
        # 0,1 sn aktif, 0,4 sn boş; 10'luk 4 parti arasında 3 sn duraklama. Ajan doğru sayar.
        for parti, parti_sayisi in ((10, 4), (20, 2), (10, 10)):
            with self.subTest(parti=parti, parti_sayisi=parti_sayisi):
                sinyal, bitis, gercek = _partili(0.10, 0.40, parti, parti_sayisi, 3.0)
                ozet, mevcut, onerilen = _cozumle(_kayit(sinyal, bitis))
                self.assertEqual((ozet["cevrim"], ozet["kopma"][0]), (gercek, 0))
                self.assertEqual((mevcut, onerilen), (gercek, gercek))
                self.assertAlmostEqual(ozet["aktif"][2], 0.10, places=2)  # birleşmiş "4,6 sn" yok
                self.assertIsNotNone(ozet["oneri"])

    def test_sinyal_bosluktan_uzun_hizli_makinede_de_birlesmez(self):
        # 0,3 sn aktif, 0,1 sn boş, 10'luk 6 parti: boşluk hem kısa kümede hem
        # sinyalden kısa, ama sinyalin 1/KUME_ORANI'sından uzun: kopma değil.
        sinyal, bitis, gercek = _partili(0.30, 0.10, 10, 6, 3.0)
        ozet, _, _ = _cozumle(_kayit(sinyal, bitis))
        self.assertEqual((ozet["cevrim"], ozet["kopma"][0]), (gercek, 0))

    def test_eksik_sayan_hizli_makine_is_sayisi_olmadan_onaylanmaz(self):
        # 0,05 sn aktif, 0,15 sn boş (öndeğer REARM 0,20 boşluktan uzun), 10'luk 12 parti.
        sinyal, bitis, gercek = _partili(0.05, 0.15, 10, 12, 3.0)
        ozet, mevcut, onerilen = _cozumle(_kayit(sinyal, bitis))
        self.assertEqual((gercek, ozet["cevrim"], mevcut, onerilen), (120, 120, 12, 120))
        self.assertTrue(ozet["hizli"])
        metin = _ozet_metni(ozet, mevcut, onerilen)
        self.assertIn("farkli", metin)
        self.assertIn("makine cok hizli", metin)
        self.assertIn("Kontrol icin is sayisini girin", metin)
        self.assertNotIn("AYNEN", metin)
        self.assertEqual(_acik_ayar_satirlari(metin), [])
        metin = _ozet_metni(ozet, mevcut, onerilen, is_sayisi=120)
        self.assertIn("Ajan MEVCUT ayarla 12 sayiyor: 108 EKSIK", metin)
        self.assertIn("AYNEN", metin)
        self.assertIn("UDAR_PULSE_REARM_SECONDS=0.07", _acik_ayar_satirlari(metin))

    def test_kisa_darbede_uzun_basis_gercek_darbeleri_parazit_yapmaz(self):
        for uzunlar in ({12}, {3, 9, 15, 21}):
            with self.subTest(uzun_basis=len(uzunlar)):
                sinyal, bitis, gercek = _kisa_darbe(uzunlar)
                ozet, mevcut, onerilen = _cozumle(_kayit(sinyal, bitis))
                self.assertEqual((ozet["cevrim"], ozet["parazit"][0]), (gercek, 0))
                self.assertEqual((mevcut, onerilen), (gercek, gercek))
                metin = _ozet_metni(ozet, mevcut, onerilen, is_sayisi=gercek)
                self.assertIn("AYNEN", metin)
                self.assertNotIn("kisa parazit", metin)

    def test_parazit_isten_kalabaliksa_ayiklanmaz_kablo_gosterilir(self):
        # İş başına iki kısa parazit (motor kalkışı gibi): parazit gerçek işten
        # kalabalık. Hangisinin gerçek olduğunu dağılım söyleyemez; ayıklanmaz.
        olaylar = []
        for sira in range(20):
            bas = 1.0 + 3.0 * sira
            olaylar += [(bas, bas + 0.3), (bas + 1.2, bas + 1.212), (bas + 2.0, bas + 2.009)]
        ozet, mevcut, onerilen = _cozumle(_kayit(_olaylardan(olaylar), 62.0))
        self.assertEqual((ozet["cevrim"], ozet["parazit"][0], mevcut), (60, 0, 20))
        metin = _ozet_metni(ozet, mevcut, onerilen, is_sayisi=20)
        self.assertIn("Ajan MEVCUT ayarla DOGRU sayiyor (20)", metin)
        self.assertIn("isten bagimsiz sinyal (parazit) var", metin)
        self.assertEqual(_acik_ayar_satirlari(metin), [])

    def test_kontak_cirpmasi_ana_sinyalle_tek_cevrimdir(self):
        # Bırakışta ya da basışta 3 kez 10 ms açık / 12 ms kapalı çırpma: çok kısa
        # parçalar ve boşluklar ana sinyale (0,25 sn) göre kısadır, tek çevrim olur.
        for yer in ("birakista", "basista"):
            olaylar = []
            for sira in range(20):
                bas = 1.0 + 2.0 * sira
                if yer == "birakista":
                    olaylar.append((bas, bas + 0.25))
                    olaylar += [(bas + 0.262 + 0.022 * k, bas + 0.272 + 0.022 * k) for k in range(3)]
                else:
                    olaylar += [(bas + 0.022 * k, bas + 0.010 + 0.022 * k) for k in range(3)]
                    olaylar.append((bas + 0.066, bas + 0.33))
            with self.subTest(yer=yer):
                ornekler = _kayit(_olaylardan(olaylar), 42.0)
                self.assertEqual(len(diagnose_gpio.olcum_parcalari(ornekler, True)), 80)
                ozet, mevcut, onerilen = _cozumle(ornekler)
                self.assertEqual((ozet["cevrim"], ozet["kopma"][0], ozet["parazit"][0]), (20, 60, 0))
                self.assertEqual((mevcut, onerilen), (20, 20))

    def test_tek_uzun_deger_kumeyi_belirleyemez(self):
        esik = diagnose_gpio.kisa_kume_esigi
        # Hızlı makine: 36 gerçek boşluk (0,4) ve 3 duraklama (3,4).
        self.assertIsNone(esik([0.4] * 36 + [3.4] * 3, diagnose_gpio.KOPMA_UST_SN))
        # Kısa darbe: 24 gerçek darbe (0,05) ve 1-4 uzun basış (0,6).
        for uzun in (1, 4):
            with self.subTest(uzun=uzun):
                self.assertIsNone(esik([0.05] * 24 + [0.6] * uzun, diagnose_gpio.PARAZIT_UST_SN, azinlik=True))
        # Uzun küme kalabalık ama kısa küme ondan kalabalık: parazit sayılmaz.
        self.assertIsNone(esik([0.008] * 21 + [0.3] * 20, diagnose_gpio.PARAZIT_UST_SN, azinlik=True))
        self.assertAlmostEqual(esik([0.008] * 5 + [0.3] * 20, diagnose_gpio.PARAZIT_UST_SN, azinlik=True), 0.008)
        # İki küme arasındaki tek ara değer ayrımı bozamaz: gerçek kümenin alt %10'una
        # bakılır, en kısa değerine değil.
        self.assertAlmostEqual(esik([0.016] * 20 + [0.05] + [2.7] * 19, diagnose_gpio.KOPMA_UST_SN), 0.05)
        self.assertAlmostEqual(esik([0.008] * 5 + [0.02] + [0.3] * 19, diagnose_gpio.PARAZIT_UST_SN, azinlik=True), 0.02)

    def test_is_sayisi_girilmezse_aynen_denmez(self):
        ozet = diagnose_gpio.olcum_ozeti(_cevrimler(20, aralik=3.0, aktif=0.3))
        metin = _ozet_metni(ozet, 20, 20)
        self.assertNotIn("AYNEN", metin)
        self.assertIn("Kontrol icin is sayisini girin", metin)
        self.assertEqual(_acik_ayar_satirlari(metin), [])
        self.assertNotIn("install.sh 'en kisa gercek cevrim' sorusunun cevabi", metin)
        metin = _ozet_metni(ozet, 20, 20, is_sayisi=20)
        self.assertIn("AYNEN", metin)
        self.assertEqual(len(_acik_ayar_satirlari(metin)), 4)


class KayitSekliTests(unittest.TestCase):
    """Kaydın şekli: sunucu 'hangi ajan ölçtü?' kararını buna dayandırır (README
    "Kaydı hangi ajan ölçtü?").

    FixA3-pi doğrulayıcı bulgusu: "'pulse' alanı yoksa 391b844 öncesi ajan"
    kuralı süre modundaki cihazları yanlış sınıflıyordu; süre kaydında 'pulse'
    alanı hiçbir sürümde yoktur. Kural ``counter.mode == 'pulse'`` ile sınırlıdır.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.saat = [0.0]

    def tearDown(self):
        self._tmp.cleanup()

    def _olc(self, config, sinyal, bitis):
        saat = self.saat

        class SahtePin:
            def __init__(self, *args, **kwargs):
                pass

            @property
            def value(self):
                return int(sinyal(saat[0]))

        class SahteZaman:
            @staticmethod
            def monotonic():
                return saat[0]

        class SanalDurdurma:
            def is_set(self):
                return saat[0] >= bitis

            def wait(self, sure):
                saat[0] = round(saat[0] + sure, 6)
                return self.is_set()

        kuyruk = PersistentQueue(Path(self._tmp.name) / f"{config.measurement_mode}.sqlite3",
                                 boot_id="test", monotonic=lambda: saat[0])
        calistir = udar_pi_agent.run_pulse_mode if config.measurement_mode == "pulse" else udar_pi_agent.run_duration_mode
        with mock.patch.object(udar_pi_agent, "DigitalInputDevice", SahtePin), \
                mock.patch.object(udar_pi_agent, "time", SahteZaman), \
                contextlib.redirect_stdout(io.StringIO()):
            calistir(config, kuyruk, SanalDurdurma())
        with sqlite3.connect(kuyruk.db_path) as conn:
            return [json.loads(satir[0]) for satir in conn.execute("SELECT payload FROM events ORDER BY id")]

    def test_pulse_alani_yalniz_vurus_kaydinda_mod_her_kayitta(self):
        # Üç çevrim: 1 sn'de bir 0,3 sn aktif.
        def sinyal(an):
            return 1.0 <= an < 4.0 and (an - 1.0) % 1.0 < 0.3

        vurus = self._olc(Config(crm_url="https://ornek.test", device_token="test"), sinyal, 5.0)
        self.saat[0] = 0.0
        sure = self._olc(
            Config(crm_url="https://ornek.test", device_token="test", measurement_mode="duration",
                   poll_interval_seconds=0.01, duration_dropout_grace_seconds=0.2),
            sinyal, 5.0,
        )
        self.assertEqual(len(vurus), 3)
        self.assertEqual(len(sure), 3)
        for kayit in vurus:
            self.assertEqual(kayit["counter"]["mode"], "pulse")
            self.assertIn("pulse", kayit)
            self.assertEqual(kayit["measured_by_version"], AGENT_VERSION)
        for kayit in sure:
            # Süre kaydında 'pulse' alanı yoktur: yokluğu burada eski ajan demek DEĞİLDİR.
            self.assertEqual(kayit["counter"]["mode"], "duration")
            self.assertNotIn("pulse", kayit)
            self.assertIs(kayit["duration"]["validated_cycle"], True)
            self.assertEqual(kayit["measured_by_version"], AGENT_VERSION)

    def test_belgedeki_eski_ajan_kurali_olcum_moduna_bagli(self):
        metin = (BURASI / "README.md").read_text(encoding="utf-8")
        self.assertIn("### Kaydı hangi ajan ölçtü?", metin)
        bolum = metin.split("### Kaydı hangi ajan ölçtü?", 1)[1].split("\n## ", 1)[0]
        for gerekli in ("`counter.mode`", "`pulse` alanı hiçbir sürümde", "391b844", "4a90800",
                        "`measured_by_version`"):
            with self.subTest(gerekli=gerekli):
                self.assertIn(gerekli, bolum)
        ajan = (BURASI / "udar_pi_agent.py").read_text(encoding="utf-8")
        self.assertIn("counter.mode == 'pulse'", ajan)


class BounceNotuTests(unittest.TestCase):
    """UDAR_BOUNCE_SECONDS notu tarihsel olarak doğru: 391b844 öncesi ajan onu kullanıyordu."""

    def test_hicbir_zaman_kullanilmadi_denmez(self):
        yanlislar = ("hicbir zaman kullan", "hiçbir zaman kullan", "hic kullanmadi", "hic etkisi yoktu")
        for dosya in ("README.md", "udar-pi-agent.env.example", "install.sh", "udar_pi_agent.py"):
            metin = (BURASI / dosya).read_text(encoding="utf-8")
            for yanlis in yanlislar:
                with self.subTest(dosya=dosya, yanlis=yanlis):
                    self.assertNotIn(yanlis, metin)

    def test_not_tarihi_ve_eski_kullanimi_soyler(self):
        for dosya in ("README.md", "udar-pi-agent.env.example"):
            metin = (BURASI / dosya).read_text(encoding="utf-8")
            with self.subTest(dosya=dosya):
                self.assertIn("Temmuz 2026", metin)
                self.assertIn("391b844", metin)
                self.assertIn("bounce_time", metin)


if __name__ == "__main__":
    unittest.main()
