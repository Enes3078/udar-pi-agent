#!/usr/bin/env python3
"""GPIO sinyal teşhisi ve ÖLÇÜME dayalı ayar önerisi.

Üç iş yapar:
1. Pindeki her 0/1 değişimini zamanıyla yazar (ham görüntü). Makine
   KAPALIYKEN tek bir ``SAYILDI`` satırı bile çıkmamalı; çıkıyorsa giriş boşta
   (yüzüyor) ya da parazit alıyor (README: Kablolama kontrol listesi).
2. Aynı sinyali ajanın süzgecinden (udar_pi_agent.PulseCycleDetector, tek
   kaynak) geçirir: ajan bu ayarlarla kaç vuruş sayardı?
3. Ctrl+C ile çıkınca ölçüm özetini ve önerilen ayarları yazar. Ayar makine
   türüne göre değil, bu makinenin gerçekte ürettiği sinyale göre seçilir.

Öneri tek bir ölçüme değil SAĞLAM istatistiğe dayanır (README "Ayarı ölçerek
bulmak"):
- Çevrimin ortasındaki kısa kopma (röle / limit şalteri sıçraması) çevrimin
  içinde sayılır; gerçek sinyalden belirgin biçimde kısa parazit çevrim sayılmaz.
  İkisi de değerlerin DAĞILIMINA bakar (iki küme ayrımı, alt %10, en az
  KUME_EN_AZ gerçek değer); tek bir duraklama ya da tek bir uzun basış bütün
  işleri "kopma" ya da "parazit" yapamaz.
- Öneriler en kısa değerden değil alt %10'dan hesaplanır: en kısa değerlerin
  %10'u (en az biri) dışarıda kalır, tek aykırı ölçüm öneriyi belirleyemez.
- Yeterli gerçek çevrim yoksa öneri verilmez.
- Önerilen ayarla ajanın kaç sayacağı AYNI kayıt üzerinde yeniden hesaplanır.
  "Aynen yapıştırın" YALNIZ gerçek iş sayısı verildiyse ve önerilen ayarla sayım
  ona eşitse denir. İş sayısı verilmezse "kontrol için iş sayısını girin" denir.
- Ajanın sayısı ölçümden farklıysa hangisinin doğru olduğunu gerçek iş sayısı
  söyler (``--is-sayisi`` ya da çıkışta sorulur); araç ajanı kendiliğinden
  hatalı ilan etmez.

Pin, pull-up, kenar ve süzgeç ayarları /etc/udar-pi-agent.env'den okunur (dosya
yalnız root'a açık: ``sudo`` ile çalıştırın); komut satırı onları ezer. Ajan
servisi çalışırken pin meşgul olabilir: önce ``sudo systemctl stop udar-pi-agent``.
"""
from __future__ import annotations

import argparse
import math
import statistics
import sys
import time
from dataclasses import fields
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from gpiozero import DigitalInputDevice
except ImportError as exc:
    raise SystemExit("python3-gpiozero gerekli. Kur: sudo apt install python3-gpiozero") from exc

# Süzgeç ve öneri kuralı ajandan gelir (bu dosya ajanın yanında durur:
# /opt/udar-pi-agent/ ve depo klasörü). Kural iki yerde yazılırsa ayrışır.
from udar_pi_agent import (
    ENV_DOSYASI,
    FAST_CYCLE_SECONDS,
    Config,
    PulseCycleDetector,
    min_interval_suggestion,
    pulse_active_level,
)

# Ölçüm süzgeci: bundan kısa aktif ya da boş parçalar (kontak sıçraması,
# kıvılcım) hiç görülmez. Ajanın ayarından bağımsızdır; amaç makinenin
# gerçek sinyalini görmek.
OLCUM_PARAZIT_SN = 0.005

# Ayıklama (olcum_ozeti). Ölçülen değerler iki kümeye ayrılır (kisa_kume_esigi):
# kısa küme ancak DAĞILIM bunu gösteriyorsa ayıklanır, tek bir değer göstermez:
# - gerçek (uzun) küme en az KUME_EN_AZ değerden oluşur: bir duraklama ya da bir
#   uzun basış tek başına "gerçek" sayılıp bütün öbür değerleri kısa yapamaz;
# - kısa kümenin en uzunu, gerçek kümenin ALT %10'undan (alt_yuzdelik) en az
#   KUME_ORANI kat kısadır: gerçek kümenin en kısa bir-iki değeri de karar veremez.
# Ayıklanan iki şey:
# - kısa kopma : iki aktif parça arasındaki boşluk KOPMA_UST_SN'den kısa, kısa
#   kümede ve çevrimin en uzun kesintisiz parçasından en az KUME_ORANI kat kısaysa
#   sinyal çevrimin içinde bir an kopmuştur (röle / şalter sıçraması); parçalar
#   TEK çevrimdir. Kopma, kestiği sinyalden kısa olur: hızlı makinede işler arası
#   boşluk sinyalden uzundur, kopma sanılmaz. Üst sınır REARM önerisinin üst
#   sınırıdır: daha uzun kopmayı ajan zaten yutamaz.
# - kısa parazit: aktif süresi PARAZIT_UST_SN'den kısa, kısa kümede ve parazitler
#   gerçek çevrimlerden AZ ise çevrim sayılmaz. Kısa darbeli sensörde tek tük
#   uzun basış gerçek darbeleri parazit yapamaz; parazit işten kalabalıksa hat
#   bozuktur, ayıklanmaz, fark özette görünür.
# Ayrım yoksa (değerler sürekli dağılıyorsa, ör. operatör temposu) hiçbir şey
# ayıklanmaz. Ayıklanan her şey özette sayısı ve en uzunuyla yazılır.
KUME_ORANI = 4.0
KUME_EN_AZ = 5
KOPMA_UST_SN = 0.50
PARAZIT_UST_SN = 0.10

# Öneri alt yüzdelikten hesaplanır ve en az EN_AZ_CEVRIM gerçek çevrim ister.
ALT_YUZDELIK = 0.10
EN_AZ_CEVRIM = 10
ONERILEN_CEVRIM = 20

# Yeniden oynatma kaydının sınırı (her değişim en çok iki örnek). Bu kadar sık
# değişen giriş ayarla düzelmez; bellek dolmasın diye kayıt burada durur.
MAKS_ORNEK = 200_000

VARSAYILAN_PIN = 27  # fiziksel pin 13

_ONDEGER = {alan.name: alan.default for alan in fields(Config)}

NEDENLER = {
    "active_too_long": "aktif cok uzun (takili sinyal?)",
    "rate_limited": "onceki vurusa cok yakin",
}

# Gerçek iş sayısı olmadan hiçbir öneri "aynen yapıştırın" denmez; bu satır denir.
IS_SAYISI_ISTE = (
    "Kontrol icin is sayisini girin: isleri sayarak olcumu tekrarlayin, cikista sorulunca "
    "yazin (ya da --is-sayisi N verin)"
)

# Öneri anahtarı -> (ayar sözlüğündeki alan, Config alanı, büyüdükçe süzgeç SIKILAŞIR mı)
AYAR_ALANI = {
    "UDAR_PULSE_MIN_ACTIVE_SECONDS": ("min_active", "pulse_min_active_seconds", True),
    "UDAR_PULSE_REARM_SECONDS": ("rearm", "pulse_rearm_seconds", True),
    "UDAR_PULSE_MAX_ACTIVE_SECONDS": ("max_active", "pulse_max_active_seconds", False),
    "UDAR_PULSE_MIN_INTERVAL_SECONDS": ("min_interval", "pulse_min_interval_seconds", True),
}

ONERI_ACIKLAMASI = {
    "UDAR_PULSE_MIN_ACTIVE_SECONDS": "kesintisiz aktif surenin alt %10'unun yarisi",
    "UDAR_PULSE_REARM_SECONDS": "bekleme suresinin alt %10'unun yarisi (en cok 0.5)",
    "UDAR_PULSE_MAX_ACTIVE_SECONDS": "en uzun aktif surenin 2 kati (en az 10)",
    "UDAR_PULSE_MIN_INTERVAL_SECONDS": "iki cevrim arasinin alt %10'unun yarisi, en az 0.20 (cok hizli makinede yarisi)",
}


def ayar_dosyasini_oku(yol: Path) -> dict[str, str] | None:
    """KEY=VALUE satırları; dosya yoksa ya da okunamıyorsa None."""
    try:
        metin = yol.read_text(encoding="utf-8")
    except OSError:
        return None
    ayarlar: dict[str, str] = {}
    for satir in metin.splitlines():
        satir = satir.strip()
        if not satir or satir.startswith("#") or "=" not in satir:
            continue
        anahtar, deger = satir.split("=", 1)
        deger = deger.strip()
        if len(deger) >= 2 and deger[0] == deger[-1] and deger[0] in "\"'":
            deger = deger[1:-1]
        ayarlar[anahtar.strip()] = deger
    return ayarlar


def _dosyadan_sayi(dosya: dict[str, str], anahtar: str, ondeger: float) -> float:
    deger = dosya.get(anahtar, "").strip()
    if not deger:
        return ondeger
    try:
        return float(deger)
    except ValueError:
        print(f"Uyari: {anahtar}={deger!r} sayi degil; {ondeger} kullaniliyor.")
        return ondeger


def ayarlari_belirle(args: argparse.Namespace, dosya: dict[str, str] | None) -> dict[str, Any]:
    """Öncelik: komut satırı > ayar dosyası > ajanın öndeğeri."""
    d = dosya or {}

    def sayi(arg_degeri, anahtar, ondeger):
        return float(arg_degeri) if arg_degeri is not None else _dosyadan_sayi(d, anahtar, ondeger)

    if args.pull_up is not None:
        pull_up = bool(args.pull_up)
    else:
        pull_up = d.get("UDAR_PULL_UP", "").strip().lower() in {"1", "true", "yes", "on"}
    edge = args.edge or d.get("UDAR_PULSE_EDGE", "").strip().lower() or "rising"
    if edge not in {"rising", "falling", "both"}:
        print(f"Uyari: UDAR_PULSE_EDGE={edge!r} gecersiz; rising kullaniliyor.")
        edge = "rising"
    return {
        "pin": int(sayi(args.pin, "UDAR_GPIO_BCM", VARSAYILAN_PIN)),
        "pull_up": pull_up,
        "edge": edge,
        "min_active": sayi(args.min_active, "UDAR_PULSE_MIN_ACTIVE_SECONDS", _ONDEGER["pulse_min_active_seconds"]),
        "rearm": sayi(args.rearm, "UDAR_PULSE_REARM_SECONDS", _ONDEGER["pulse_rearm_seconds"]),
        "max_active": sayi(args.max_active, "UDAR_PULSE_MAX_ACTIVE_SECONDS", _ONDEGER["pulse_max_active_seconds"]),
        "min_interval": sayi(args.min_interval, "UDAR_PULSE_MIN_INTERVAL_SECONDS", _ONDEGER["pulse_min_interval_seconds"]),
    }


def _asagi(deger: float, basamak: int) -> float:
    kat = 10 ** basamak
    return math.floor(round(deger * kat, 6)) / kat


# ---------------------------------------------------------------------------
# Kayıt ve yeniden oynatma
# ---------------------------------------------------------------------------


class OrnekKaydi:
    """Süzgeci sonradan AYNEN yeniden oynatmaya yetecek (an, değer) örnekleri.

    Yalnız ilk örnek, her değişim anı, her değişimden hemen önceki örnek ve son
    örnek tutulur. PulseCycleDetector iki değişim arasındaki aynı değerli
    örneklerden yalnız sonuncusuna bakmakla aynı sonuca varır: zamanları
    ``candidate_since``'ten alır ve kararlılık süresi en uzun o örnekte olur.
    Bu yüzden önerilen ayarla sayım, bütün örneklerle yapılmışçasına kesindir.
    Çok gürültülü girişte bellek dolmasın diye ``sinir``da durur (``kesildi``).
    """

    def __init__(self, sinir: int = MAKS_ORNEK):
        self.ornekler: list[tuple[float, bool]] = []
        self.kesildi = False
        self._sinir = sinir
        self._son: tuple[float, bool] | None = None

    def ekle(self, an: float, deger: bool) -> None:
        ornek = (an, bool(deger))
        if self._son is None:
            self._tut(ornek)
        elif ornek[1] != self._son[1]:
            self._tut(self._son)
            self._tut(ornek)
        self._son = ornek

    def bitir(self) -> list[tuple[float, bool]]:
        if self._son is not None:
            self._tut(self._son)
        return self.ornekler

    def _tut(self, ornek: tuple[float, bool]) -> None:
        if self.kesildi or (self.ornekler and self.ornekler[-1] == ornek):
            return
        if len(self.ornekler) >= self._sinir:
            self.kesildi = True
            return
        self.ornekler.append(ornek)


def _kabul_edilenler(ornekler, dedektor: PulseCycleDetector):
    for an, deger in ornekler:
        sonuc = dedektor.feed(deger, an)
        if sonuc and sonuc.get("accepted"):
            yield sonuc


def olcum_parcalari(ornekler, active_level: bool) -> list[tuple[float, float]]:
    """Örneklerden aktif parçalar (başı, sonu; monoton sn), ayıklanmadan.

    Yalnız OLCUM_PARAZIT_SN'den kısa sıçramalar görülmez. Kopmalar ve
    parazitler olcum_ozeti'nde ayıklanır.
    """
    olcum = PulseCycleDetector(
        active_level=active_level,
        min_active_seconds=OLCUM_PARAZIT_SN,
        rearm_seconds=OLCUM_PARAZIT_SN,
        max_active_seconds=0.0,
        min_interval_seconds=0.0,
    )
    return [(s["started_at"], s["ended_at"]) for s in _kabul_edilenler(ornekler, olcum)]


def ajan_sayimi(ornekler, ayar: dict[str, Any]) -> int:
    """Ajan bu ayarlarla bu kayıtta kaç vuruş sayardı (ajanın kendi süzgeciyle)."""
    dedektor = PulseCycleDetector(
        active_level=pulse_active_level(ayar["edge"]),
        min_active_seconds=ayar["min_active"],
        rearm_seconds=ayar["rearm"],
        max_active_seconds=ayar["max_active"],
        min_interval_seconds=ayar["min_interval"],
    )
    return sum(1 for _ in _kabul_edilenler(ornekler, dedektor))


# ---------------------------------------------------------------------------
# Sağlam istatistik ve öneri
# ---------------------------------------------------------------------------


def _alt_sira(adet: int, oran: float = ALT_YUZDELIK) -> int:
    """``adet`` sıralı değerde alt yüzdeliğin sırası: en az biri dışarıda kalır."""
    return min(max(1, int(adet * oran)), adet - 1)


def alt_yuzdelik(degerler: list[float], oran: float = ALT_YUZDELIK) -> float:
    """Alt yüzdelik (en yakın sıra). En küçük değerlerin ``oran`` kadarı, EN AZ
    BİRİ dışarıda kalır: tek aykırı ölçüm (bir sıçrama, bir kopma) belirleyemez.
    Tek değer varsa odur."""
    sirali = sorted(degerler)
    return sirali[_alt_sira(len(sirali), oran)]


def kisa_kume_esigi(degerler: list[float], ust_sinir: float, *, azinlik: bool = False) -> float | None:
    """Kısa değerleri gerçek (uzun) değerlerden ayıran eşik; ayrım yoksa None.

    İki küme ayrımı: sıralı değerler her olası noktadan kısa ve uzun iki kümeye
    bölünür. Bir bölme ancak şu hâlde geçerlidir:
    - kısa kümenin hepsi ``ust_sinir``dan kısa,
    - uzun (gerçek) küme en az KUME_EN_AZ değer: bir-iki uzun değer ayrımı
      belirleyemez,
    - uzun kümenin ALT %10'u kısa kümenin en uzunundan en az KUME_ORANI kat
      büyük: uzun kümenin en kısa bir-iki değeri de belirleyemez,
    - ``azinlik`` istenirse kısa küme uzun kümeden az.
    Geçerli bölmelerden iki kümenin sınırındaki sıçraması en büyük olan seçilir
    (doğal sınır); dönen eşik kısa kümenin en uzunudur (o değer ve altındakiler
    kısa kümededir).
    """
    sirali = sorted(degerler)
    adet = len(sirali)
    esik = None
    en_buyuk_sicrama = 0.0
    for sira in range(1, adet):  # kısa küme: sirali[:sira], uzun küme: sirali[sira:]
        kisa_en_uzun = sirali[sira - 1]
        uzun_adet = adet - sira
        if kisa_en_uzun >= ust_sinir or uzun_adet < KUME_EN_AZ:
            break  # sıra büyüdükçe ikisi de kötüleşir
        if kisa_en_uzun <= 0 or sirali[sira] == kisa_en_uzun:
            continue  # eşit değerler aynı kümede kalır
        if azinlik and sira >= uzun_adet:
            continue
        if sirali[sira + _alt_sira(uzun_adet)] < KUME_ORANI * kisa_en_uzun:
            continue
        sicrama = sirali[sira] / kisa_en_uzun
        if sicrama > en_buyuk_sicrama:
            esik, en_buyuk_sicrama = kisa_en_uzun, sicrama
    return esik


def _kopmalari_birlestir(parcalar):
    """Kısa kopmayla bölünmüş parçaları tek çevrimde toplar.

    1. Boşluk dağılımının kısa kümesindeki (kisa_kume_esigi) boşluklarla bağlı
       parçalar aday çevrimdir.
    2. Kopma, kestiği sinyalden kısa olur: adayın içindeki her boşluk adayın en
       uzun kesintisiz parçasından en az KUME_ORANI kat kısa olmalı. Uzun kalan
       boşluktan aday bölünür ve parçalar yeniden sınanır. Hızlı makinede işler
       arası boşluk sinyalden uzundur; duraklamalar yüzünden kısa kümede kalsa da
       işler birleşmez. Bırakıştaki kontak çırpması (çok kısa parçalar, çok kısa
       boşluklar) ana sinyale göre kısadır; ana sinyalle tek çevrim olur.

    Dönen çevrim: [başı, sonu, en uzun KESİNTİSİZ parça]. Ajan aktifliğin
    kesintisiz MIN_ACTIVE kadar sürmesini ister; kopmalı çevrimde belirleyici
    olan toplam süre değil en uzun kesintisiz parçadır.
    """
    bosluklar = [sonraki[0] - onceki[1] for onceki, sonraki in zip(parcalar, parcalar[1:])]
    esik = kisa_kume_esigi(bosluklar, KOPMA_UST_SN)
    adaylar: list[tuple[int, int]] = []  # (ilk parça, son parça) sıraları
    for sira in range(len(parcalar)):
        if adaylar and esik is not None and bosluklar[sira - 1] <= esik:
            adaylar[-1] = (adaylar[-1][0], sira)
        else:
            adaylar.append((sira, sira))

    cevrimler: list[list[float]] = []
    kopmalar: list[float] = []
    bekleyen = adaylar[::-1]  # sırayı koruyan yığın
    while bekleyen:
        ilk, son = bekleyen.pop()
        en_uzun = max(parcalar[i][1] - parcalar[i][0] for i in range(ilk, son + 1))
        kesimler = [i for i in range(ilk + 1, son + 1) if bosluklar[i - 1] * KUME_ORANI > en_uzun]
        if kesimler:
            sinirlar = [ilk, *kesimler, son + 1]
            bekleyen.extend((sinirlar[k], sinirlar[k + 1] - 1) for k in reversed(range(len(sinirlar) - 1)))
            continue
        cevrimler.append([parcalar[ilk][0], parcalar[son][1], en_uzun])
        kopmalar.extend(bosluklar[ilk:son])
    return cevrimler, kopmalar


def olcum_ozeti(parcalar: list[tuple[float, float]]) -> dict[str, Any]:
    """Ölçülen aktif parçalardan (başı, sonu; monoton sn) ayıklama, özet ve öneri.

    1. Kısa kopmalar birleştirilir, kısa parazitler ayıklanır (bkz. KUME_ORANI;
       parazit ancak gerçek çevrimlerden azsa ayıklanır).
    2. Kalan GERÇEK çevrimlerden alt %10 (alt_yuzdelik) ile öneri:
       - MIN_ACTIVE  : kesintisiz en uzun parçanın alt %10'unun yarısı (en az 0,005).
       - REARM       : beklemenin alt %10'unun yarısı (0,01 ile 0,50 arası).
       - MAX_ACTIVE  : en uzun aktif sürenin 2 katı (en az 10). Kısa kalırsa
         gerçek iş sayılmaz; bu yüzden en uzun değere bakılır.
       - MIN_INTERVAL: iki çevrim arasının alt %10'una göre min_interval_suggestion
         (install.sh ile aynı kural).
    3. Öneri ayıklananı gerçekten ayırt edemiyorsa (MIN_ACTIVE en uzun parazitten,
       REARM en uzun kopmadan kısa kalıyorsa) o ayar önerilmez: değeri None,
       nedeni ``engel``de.
    EN_AZ_CEVRIM'den az gerçek çevrim varsa ``oneri`` None.
    """
    cevrimler, kopmalar = _kopmalari_birlestir(list(parcalar))
    sureler = [bitis - bas for bas, bitis, _ in cevrimler]
    parazit_esigi = kisa_kume_esigi(sureler, PARAZIT_UST_SN, azinlik=True)
    parazitler = [s for s in sureler if parazit_esigi is not None and s <= parazit_esigi]
    gercek = [c for c, s in zip(cevrimler, sureler) if parazit_esigi is None or s > parazit_esigi]

    aktifler = [bitis - bas for bas, bitis, _ in gercek]
    kesintisiz = [en_uzun for _, _, en_uzun in gercek]
    beklemeler = [sonraki[0] - onceki[1] for onceki, sonraki in zip(gercek, gercek[1:])]
    araliklar = [sonraki[1] - onceki[1] for onceki, sonraki in zip(gercek, gercek[1:])]

    ozet: dict[str, Any] = {
        "parca": len(parcalar),
        "cevrim": len(gercek),
        "kopma": (len(kopmalar), max(kopmalar) if kopmalar else 0.0),
        "parazit": (len(parazitler), max(parazitler) if parazitler else 0.0),
        "aktif": None,
        "kesintisiz_alt": None,
        "bekleme": None,
        "aralik": None,
        "hizli": False,
        "oneri": None,
        "engel": {},
    }
    if aktifler:
        ozet["aktif"] = (alt_yuzdelik(aktifler), statistics.median(aktifler), max(aktifler))
        ozet["kesintisiz_alt"] = alt_yuzdelik(kesintisiz)
    if beklemeler:
        ozet["bekleme"] = (alt_yuzdelik(beklemeler), statistics.median(beklemeler))
        ozet["aralik"] = (alt_yuzdelik(araliklar), statistics.median(araliklar))
        ozet["hizli"] = ozet["aralik"][0] <= FAST_CYCLE_SECONDS
    if len(gercek) < EN_AZ_CEVRIM:
        return ozet

    oneri: dict[str, float | None] = {
        "UDAR_PULSE_MIN_ACTIVE_SECONDS": max(0.005, _asagi(ozet["kesintisiz_alt"] / 2, 3)),
        "UDAR_PULSE_REARM_SECONDS": min(0.50, max(0.01, _asagi(ozet["bekleme"][0] / 2, 2))),
        "UDAR_PULSE_MAX_ACTIVE_SECONDS": max(10.0, float(math.ceil(max(aktifler) * 2))),
        "UDAR_PULSE_MIN_INTERVAL_SECONDS": min_interval_suggestion(ozet["aralik"][0]),
    }
    if parazitler and oneri["UDAR_PULSE_MIN_ACTIVE_SECONDS"] <= ozet["parazit"][1]:
        oneri["UDAR_PULSE_MIN_ACTIVE_SECONDS"] = None
        ozet["engel"]["UDAR_PULSE_MIN_ACTIVE_SECONDS"] = (
            "parazit gercek sinyale cok yakin; ayar degil kablolama sorunu"
        )
    if kopmalar and oneri["UDAR_PULSE_REARM_SECONDS"] <= ozet["kopma"][1]:
        oneri["UDAR_PULSE_REARM_SECONDS"] = None
        ozet["engel"]["UDAR_PULSE_REARM_SECONDS"] = (
            "sinyal kopmasi gercek beklemeye cok yakin; ayar degil kablolama sorunu"
        )
    ozet["oneri"] = oneri
    return ozet


def oneri_ayari(ayar: dict[str, Any], oneri: dict[str, float | None] | None) -> dict[str, Any]:
    """Önerilen değerler uygulanmış ayar; önerisi olmayan ayar olduğu gibi kalır."""
    sonuc = dict(ayar)
    for anahtar, deger in (oneri or {}).items():
        if deger is not None:
            sonuc[AYAR_ALANI[anahtar][0]] = deger
    return sonuc


def gevseten_oneriler(oneri: dict[str, float | None], ayar: dict[str, Any]) -> dict[str, float]:
    """Mevcut ayardan YA DA öndeğerden gevşek öneriler: anahtar -> aşılan (sıkı) değer.

    Gevşek süzgeç daha çok sayar; sahte vuruş riski budur. MAX_ACTIVE'te 0 =
    sınır yok (en gevşek) demektir.
    """
    sonuc: dict[str, float] = {}
    for anahtar, deger in oneri.items():
        if deger is None:
            continue
        alan, config_alani, buyuk_siki = AYAR_ALANI[anahtar]
        mevcut, ondeger = float(ayar[alan]), float(_ONDEGER[config_alani])
        if buyuk_siki:
            referans = max(mevcut, ondeger)
            if deger < referans - 1e-9:
                sonuc[anahtar] = referans
        else:
            referans = min(v for v in (mevcut, ondeger) if v > 0)
            if deger > referans + 1e-9:
                sonuc[anahtar] = referans
    return sonuc


def oneri_engelleri(
    *,
    gevsek: bool,
    olculen: int,
    ajan_sayisi: int,
    onerilen_sayi: int | None,
    is_sayisi: int | None,
) -> list[str]:
    """Önerinin "aynen yapıştırın" denemeyeceği nedenler; boşsa yapıştırılabilir.

    Öneri YALNIZ gerçek iş sayısıyla kontrol edilince yapıştırılabilir: önerilen
    ayarla sayım gerçek iş sayısına eşit olmalı. Ölçümün ayıklaması
    (kopma/parazit) ve ölçümün kendisi makineye uymayabilir; ajanın ve ölçümün
    aynı sayıyı bulması, ikisinin de aynı işleri kaçırmadığını göstermez.
    Süzgeci gevşeten öneri ayrıca yalnız ajan mevcut ayarla gerçek işleri
    kaçırıyorsa girilir. Gerçek iş sayısı yoksa ilk neden "kontrol için iş
    sayısını girin"dir; ardından öneriyi ayrıca riskli kılan nedenler gelir.
    """
    nedenler: list[str] = []
    if is_sayisi is not None:
        if onerilen_sayi != is_sayisi:
            nedenler.append(f"onerilen ayarla ajan {onerilen_sayi} sayardi, gercek is {is_sayisi}")
        elif gevsek and ajan_sayisi == is_sayisi:
            nedenler.append(
                "ajan mevcut ayarla zaten dogru sayiyor; GEVSETIR yazan ayar gereksiz yere daha cok saydirir"
            )
        return nedenler
    nedenler.append("gercek is sayisi girilmedi; ayarlar ancak is sayisiyla kontrol edilince girilir")
    if onerilen_sayi != ajan_sayisi:
        nedenler.append(
            f"onerilen ayar sayimi degistirir (mevcut {ajan_sayisi}, onerilen {onerilen_sayi}); hangisinin "
            "dogru oldugunu gercek is sayisi soyler"
        )
    elif onerilen_sayi != olculen:
        nedenler.append(f"sayim olcumu ({olculen}) tutmuyor")
    if gevsek:
        nedenler.append(
            "GEVSETIR yazan ayar ajani daha cok saydirir; yalniz ajanin gercek isleri kacirdigi "
            "is sayisiyla gorulurse girilir"
        )
    return nedenler


# ---------------------------------------------------------------------------
# Özet metni
# ---------------------------------------------------------------------------


def _yaz(deger: float) -> str:
    return format(deger, ".3f").rstrip("0").rstrip(".")


def _ayar_metni(ayar: dict[str, Any]) -> str:
    return (
        f"min_active={_yaz(ayar['min_active'])} rearm={_yaz(ayar['rearm'])} "
        f"max_active={_yaz(ayar['max_active'])} min_interval={_yaz(ayar['min_interval'])}"
    )


def _karsilastir(ad: str, sayi: int, gercek: int) -> str:
    if sayi == gercek:
        return f"{ad} DOGRU sayiyor ({sayi})."
    fark = sayi - gercek
    return f"{ad} {sayi} sayiyor: {abs(fark)} {'FAZLA' if fark > 0 else 'EKSIK'}."


def ozet_satirlari(
    ozet: dict[str, Any],
    *,
    sure: float,
    ham_sayi: int,
    ajan_sayisi: int,
    onerilen_sayi: int | None,
    ayar: dict[str, Any],
    is_sayisi: int | None = None,
    kesildi: bool = False,
) -> list[str]:
    """Ctrl+C sonrası özet. ``ajan_sayisi`` mevcut ayarla, ``onerilen_sayi``
    önerilen ayarla aynı kayıttaki sayım; ``is_sayisi`` kullanıcının saydığı
    gerçek iş (bilinmiyorsa None; 0 = makine kapalı denemesi, öneri yok)."""
    if is_sayisi == 0:
        onerilen_sayi = None
    s: list[str] = ["", f"--- OZET ({int(sure // 60)} dk {int(sure % 60)} sn) ---"]
    s.append(f"Ham sinyal ({ayar['edge']}) SAYILDI: {ham_sayi}   <- makine KAPALIYKEN bu 0 olmali")
    if kesildi:
        s.append(f"Ajan MEVCUT ayarla sayardi: {ajan_sayisi}   [{_ayar_metni(ayar)}]")
        s.append(
            f"Sinyal cok sik degisti ({MAKS_ORNEK // 2}+ degisim). Bu bir ayar degil kablolama sorunu; "
            "oneri verilmez. README 'Kablolama kontrol listesi' 3-6. maddeler."
        )
        return s

    s.append(f"Olculen gercek cevrim: {ozet['cevrim']}")
    kopma_sayi, kopma_en_uzun = ozet["kopma"]
    if kopma_sayi:
        s.append(f"  kisa kopma   : {kopma_sayi} (en uzun {kopma_en_uzun:.3f} sn) -> ayni cevrimin parcasi sayildi")
    parazit_sayi, parazit_en_uzun = ozet["parazit"]
    if parazit_sayi:
        s.append(f"  kisa parazit : {parazit_sayi} (en uzun {parazit_en_uzun:.3f} sn) -> cevrim sayilmadi")
    s.append(f"Ajan MEVCUT ayarla sayardi  : {ajan_sayisi}   [{_ayar_metni(ayar)}]")
    if onerilen_sayi is not None:
        s.append(f"Ajan ONERILEN ayarla sayardi: {onerilen_sayi}")

    if ozet["aktif"]:
        a_alt, a_ort, a_max = ozet["aktif"]
        s.append(f"Aktif (sinyal var) sure : alt %10 {a_alt:.3f} sn, ortanca {a_ort:.3f} sn, en uzun {a_max:.3f} sn")
        if kopma_sayi:
            s.append(f"  kesintisiz en uzun parca: alt %10 {ozet['kesintisiz_alt']:.3f} sn")
    if ozet["bekleme"]:
        b_alt, b_ort = ozet["bekleme"]
        c_alt, c_ort = ozet["aralik"]
        s.append(f"Bekleme (sinyal yok)    : alt %10 {b_alt:.3f} sn, ortanca {b_ort:.3f} sn")
        s.append(f"Iki cevrim arasi        : alt %10 {c_alt:.3f} sn, ortanca {c_ort:.3f} sn")
        s.append("(alt %10: en kisa degerlerin %10'u, en az biri, disarida kalir; tek aykiri olcum oneriyi belirlemez)")

    # Sonuç: ajan ancak gerçek iş sayısıyla karşılaştırılınca "yanlış" denebilir.
    s.append("")
    if is_sayisi is not None:
        s.append(f"SONUC (gercek is sayisi {is_sayisi}, sizin saydiginiz):")
        s.append("  " + _karsilastir("Ajan MEVCUT ayarla", ajan_sayisi, is_sayisi))
        if onerilen_sayi is not None:
            s.append("  " + _karsilastir("Ajan ONERILEN ayarla", onerilen_sayi, is_sayisi))
        if ozet["cevrim"] != is_sayisi:
            if ozet["cevrim"] > is_sayisi:
                neden = "hatta isten bagimsiz sinyal (parazit) var"
            elif kopma_sayi or parazit_sayi:
                neden = "ayiklanan kisa kopma/parazit bu makinede gercek is olabilir ya da sinyal bazi islerde gelmiyor"
            else:
                neden = "sinyal bazi islerde gelmiyor"
            s.append(
                f"  Olculen cevrim ({ozet['cevrim']}) gercek is sayisini tutmuyor: {neden}. "
                "README 'Kablolama kontrol listesi'ne bakin."
            )
    else:
        # Gerçek iş sayısı yok: ajanın doğru saydığı söylenemez. Ajan ile ölçüm
        # aynı işleri birlikte kaçırabilir (ör. hızlı makinede ikisi de eksik).
        if ajan_sayisi == ozet["cevrim"]:
            s.append(
                f"SONUC: ajanin sayisi olcumle ayni ({ajan_sayisi}); gercek is sayisi girilmedigi icin "
                "dogru saydigi bilinmiyor."
            )
        else:
            s.append(
                f"SONUC: ajanin sayisi ({ajan_sayisi}) olcumden ({ozet['cevrim']}) farkli. Hangisinin dogru "
                "oldugunu makinenin gercekte yaptigi is sayisi soyler."
            )
        s.append(f"  {IS_SAYISI_ISTE}.")

    if is_sayisi == 0:
        s.append("Makine kapali denemesinde ayar onerilmez: bu deneme yalniz kabloyu sinar.")
        return s
    if ozet["hizli"]:
        s.append("UYARI: makine cok hizli; tek tek vurus saymak hataya acik. Mumkunse makinenin kendi sayacini kullanin.")

    oneri = ozet["oneri"]
    if oneri is None:
        s.append(
            f"Oneri icin en az {EN_AZ_CEVRIM} (tercihen {ONERILEN_CEVRIM}+) gercek cevrim gerekli; bu olcumde "
            f"{ozet['cevrim']}. Makineyi en hizli GERCEK temposunda calistirip tekrar olcun."
        )
        return s

    gevsek = gevseten_oneriler(oneri, ayar)
    nedenler = oneri_engelleri(
        gevsek=bool(gevsek),
        olculen=ozet["cevrim"],
        ajan_sayisi=ajan_sayisi,
        onerilen_sayi=onerilen_sayi,
        is_sayisi=is_sayisi,
    )
    yapistir = not nedenler

    s.append("")
    if yapistir:
        s.append("ONERILEN AYARLAR (bash install.sh --configure ya da sudo nano /etc/udar-pi-agent.env).")
        s.append(f"Gercek is sayisiyla ({is_sayisi}) kontrol edildi: satirlar ayar dosyasina AYNEN")
        s.append("yapistirilabilir. Aciklama ayri '#' satirinda: ayar dosyasinda deger satirinin sonuna")
        s.append("yorum yazilmaz, yazilirsa degerin parcasi sayilir.")
    else:
        s.append("ONERILEN AYARLAR - DOGRUDAN GIRMEYIN. Neden:")
        s.extend(f"  - {neden}." for neden in nedenler)
        s.append("Bu yuzden satirlar '#' ile kapali; yapistirilsa da bir sey degismez.")
    for anahtar, deger in oneri.items():
        if deger is None:
            s.append(f"# {anahtar}: oneri yok ({ozet['engel'][anahtar]})")
            continue
        aciklama = ONERI_ACIKLAMASI[anahtar]
        if anahtar in gevsek:
            aciklama += f"  <- GEVSETIR (simdiki ya da ondeger {_yaz(gevsek[anahtar])})"
        s.append(f"# {aciklama}")
        s.append(f"{'' if yapistir else '#'}{anahtar}={_yaz(deger)}")
    if yapistir:
        s.append(f"install.sh 'en kisa gercek cevrim' sorusunun cevabi: {_yaz(_asagi(ozet['aralik'][0], 2))}")
    s.append("Ayarlari girdikten sonra olcumu tekrarlayin: 'Ajan MEVCUT ayarla sayardi' gercek is sayisina esit olmali.")
    return s


def ozeti_yaz(ozet: dict[str, Any], **kwargs) -> None:
    for satir in ozet_satirlari(ozet, **kwargs):
        print(satir)


def _is_sayisini_sor() -> int | None:
    """Ctrl+C'den sonra gerçek iş sayısını sorar; terminal yoksa ya da geçilirse None."""
    try:
        if sys.stdin is None or not sys.stdin.isatty():
            return None
        print()
        cevap = input("Bu olcumde makine gercekte kac is yapti? (makine kapaliysa 0, bilmiyorsaniz Enter): ")
    except (EOFError, KeyboardInterrupt, OSError, ValueError):
        return None
    cevap = cevap.strip()
    if not cevap:
        return None
    try:
        sayi = int(cevap)
    except ValueError:
        print(f"'{cevap}' sayi degil; gercek is sayisi olmadan devam ediliyor.")
        return None
    return sayi if sayi >= 0 else None


def _is_sayisi_arg(deger: str) -> int:
    try:
        sayi = int(deger)
    except ValueError:
        raise argparse.ArgumentTypeError("tam sayi olmali (ornek: 20)") from None
    if sayi < 0:
        raise argparse.ArgumentTypeError("0 ya da daha buyuk olmali")
    return sayi


def main() -> int:
    parser = argparse.ArgumentParser(description="UDAR GPIO sinyal teshisi ve olcume dayali ayar onerisi")
    parser.add_argument(
        "--pin", type=int, default=None,
        help=f"BCM GPIO pin. Fiziksel pin 13 icin 27. Verilmezse ayar dosyasindan, o da yoksa {VARSAYILAN_PIN}.",
    )
    parser.add_argument("--pull-up", dest="pull_up", action="store_const", const=True, default=None,
                        help="GPIO ic pull-up kullan.")
    parser.add_argument("--no-pull-up", dest="pull_up", action="store_const", const=False,
                        help="GPIO ic pull-down kullan (ajanin ondegeri).")
    parser.add_argument(
        "--edge", choices=["rising", "falling", "both"], default=None,
        help="rising: bosta 0, vurusta 1. falling: bosta 1, vurusta 0. both: ham gorunumde her "
             "degisimi SAYILDI yazar; ajan 'both'u rising gibi sayar.",
    )
    parser.add_argument("--poll", type=float, default=0.002, help="Okuma araligi saniye.")
    parser.add_argument("--debounce", type=float, default=0.005, help="Ham gorunumde tekrar sayim filtresi saniye.")
    parser.add_argument("--min-active", type=float, default=None, help="Denenecek UDAR_PULSE_MIN_ACTIVE_SECONDS.")
    parser.add_argument("--rearm", type=float, default=None, help="Denenecek UDAR_PULSE_REARM_SECONDS.")
    parser.add_argument("--max-active", type=float, default=None, help="Denenecek UDAR_PULSE_MAX_ACTIVE_SECONDS.")
    parser.add_argument("--min-interval", type=float, default=None, help="Denenecek UDAR_PULSE_MIN_INTERVAL_SECONDS.")
    parser.add_argument(
        "--is-sayisi", type=_is_sayisi_arg, default=None,
        help="Olcum boyunca makinenin gercekte yaptigi is sayisi (siz sayin; makine kapali denemede 0). "
             "Verilirse arac ajanin dogru sayip saymadigini soyler; verilmezse cikista sorulur.",
    )
    parser.add_argument("--ayar-dosyasi", default=ENV_DOSYASI, help="Ajanin ayar dosyasi.")
    args = parser.parse_args()

    dosya = ayar_dosyasini_oku(Path(args.ayar_dosyasi))
    if dosya is None:
        print(f"Not: {args.ayar_dosyasi} okunamadi (sudo ile calistirin); ajanin ondegerleri kullaniliyor.")
    else:
        print(f"Ayarlar {args.ayar_dosyasi} dosyasindan okundu; komut satiri onlari ezer.")
    ayar = ayarlari_belirle(args, dosya)

    try:
        pin = DigitalInputDevice(ayar["pin"], pull_up=ayar["pull_up"])
    except Exception as exc:  # noqa: BLE001 (gpiozero arka uca gore farkli hata verir)
        raise SystemExit(
            f"GPIO{ayar['pin']} acilamadi: {exc}\n"
            "Ajan servisi calisiyorsa once durdurun:  sudo systemctl stop udar-pi-agent"
        ) from None

    active_level = pulse_active_level(ayar["edge"])
    ajan = PulseCycleDetector(
        active_level=active_level,
        min_active_seconds=ayar["min_active"],
        rearm_seconds=ayar["rearm"],
        max_active_seconds=ayar["max_active"],
        min_interval_seconds=ayar["min_interval"],
    )

    last = bool(pin.value)
    ham_sayi = 0
    ajan_sayisi = 0
    last_counted = float("-inf")
    baslangic = time.monotonic()
    aktif_basladi = baslangic if last == active_level else None
    kayit = OrnekKaydi()

    print(
        f"GPIO{ayar['pin']} izleniyor. pull_up={str(ayar['pull_up']).lower()} edge={ayar['edge']} "
        f"initial={int(last)} poll={args.poll}s debounce={args.debounce}s. Ctrl+C ile cik ve ozeti gor.",
        flush=True,
    )

    try:
        while True:
            value = bool(pin.value)
            now = time.monotonic()
            kayit.ekle(now, value)
            ajan_sonucu = ajan.feed(value, now)

            if value != last:
                edge = "rising" if value else "falling"
                if value == active_level:
                    aktif_basladi = now
                elif aktif_basladi is not None:
                    print(f"  aktif sure: {now - aktif_basladi:.4f} sn", flush=True)
                    aktif_basladi = None
                last = value
                should_count = ayar["edge"] == "both" or ayar["edge"] == edge
                counted = False
                if should_count and now - last_counted >= args.debounce:
                    ham_sayi += 1
                    last_counted = now
                    counted = True
                print(
                    f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] value={int(value)} edge={edge} "
                    f"count={ham_sayi}{' SAYILDI' if counted else ''}",
                    flush=True,
                )

            if ajan_sonucu:
                if ajan_sonucu.get("accepted"):
                    ajan_sayisi += 1
                    print(f"  -> AJAN SAYARDI #{ajan_sayisi} (aktif {ajan_sonucu['active_seconds']:.3f} sn)", flush=True)
                else:
                    neden = NEDENLER.get(ajan_sonucu["reason"], ajan_sonucu["reason"])
                    print(f"  -> ajan saymazdi: {neden} (aktif {ajan_sonucu['active_seconds']:.3f} sn)", flush=True)
            time.sleep(args.poll)
    except KeyboardInterrupt:
        pass

    sure = time.monotonic() - baslangic
    ornekler = kayit.bitir()
    is_sayisi = args.is_sayisi if args.is_sayisi is not None else _is_sayisini_sor()
    ozet = olcum_ozeti(olcum_parcalari(ornekler, active_level))
    onerilen_sayi = None
    if ozet["oneri"] and not kayit.kesildi and is_sayisi != 0:
        onerilen_sayi = ajan_sayimi(ornekler, oneri_ayari(ayar, ozet["oneri"]))
    ozeti_yaz(
        ozet,
        sure=sure,
        ham_sayi=ham_sayi,
        ajan_sayisi=ajan_sayisi,
        onerilen_sayi=onerilen_sayi,
        ayar=ayar,
        is_sayisi=is_sayisi,
        kesildi=kayit.kesildi,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
