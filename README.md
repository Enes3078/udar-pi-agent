# UDAR CRM Raspberry Pi Makine Ajanı

Bu ajan, makineden Raspberry Pi'a gelen 3.3V sinyali okuyup UDAR CRM üretim API'sine gönderir. İki çalışma tipi vardır:

- `pulse`: GPIO17'ye her 3.3V gelişini 1 işlem olarak sayar.
- `duration`: GPIO17 yüksek kaldığı süreyi ölçer ve süreyi üretim verisi olarak gönderir.

Her iki modda da günlük sayaç gece yeni güne geçince sıfırlanır, fakat olay geçmişi yerel SQLite logunda saklanır. İnternet veya CRM kesilirse veriler kuyruğa alınır ve tekrar gönderilir.

## Git Kurulu Makinede Sıfırdan Kurulum

Raspberry Pi OS Lite üzerinde git varsa yeni cihaza kurulumu şu komutlarla yap:

```bash
cd ~
git clone https://github.com/Enes3078/udar-pi-agent.git
cd udar-pi-agent
chmod +x install.sh update.sh diagnose_gpio.py
bash install.sh --configure
```

Script senden sırayla şunları ister:

- CRM adresi: genelde `https://crm.aykadoor.com`
- CRM cihaz tokeni: CRM > İmalat Yönetimi > Cihaz & Veri ekranındaki cihaz kartından kopyalanır.
- GPIO BCM pini:
  - fiziksel pin 13 için `27`
  - fiziksel pin 11 için `17`
- makine tipi:
  - `1`: vuruş/sayım bazlı makine
  - `2`: süre bazlı makine
- istasyon kodu: CRM'deki istasyon koduyla birebir aynı yazılır. Örn. `LZR-1`, `MKS-1`, `ABAKAN2`.

Kurulum bittikten sonra servisi başlat:

```bash
sudo systemctl restart udar-pi-agent
sudo systemctl status udar-pi-agent
journalctl -u udar-pi-agent -f
```

## Eski Kurulumu Silip Temiz Kurmak

Eski env dahil her şeyi sıfırlamak istiyorsan:

```bash
sudo systemctl stop udar-pi-agent || true
sudo systemctl disable udar-pi-agent || true
sudo rm -f /etc/systemd/system/udar-pi-agent.service
sudo rm -f /etc/udar-pi-agent.env
sudo rm -rf /opt/udar-pi-agent /var/lib/udar-pi-agent
sudo systemctl daemon-reload

rm -rf ~/udar-pi-agent
git clone https://github.com/Enes3078/udar-pi-agent.git ~/udar-pi-agent
cd ~/udar-pi-agent
chmod +x install.sh update.sh diagnose_gpio.py
bash install.sh --configure

sudo systemctl restart udar-pi-agent
journalctl -u udar-pi-agent -f
```

## Mevcut Kurulumu Güncellemek

Env dosyasını koruyarak son kodu çekmek için:

```bash
cd ~/udar-pi-agent
bash update.sh
journalctl -u udar-pi-agent -f
```

Makine tipini, pini veya tokeni yeniden ayarlamak istersen:

```bash
cd ~/udar-pi-agent
bash install.sh --configure
sudo systemctl restart udar-pi-agent
```

## Vuruş Bazlı Makine İçin Örnek Ayar

Her vuruşta kısa süreli `0 -> 1 -> 0` sinyal geliyorsa:

```env
UDAR_CRM_URL=https://crm.aykadoor.com
UDAR_DEVICE_TOKEN=CRMDEKI_CIHAZ_TOKENI

UDAR_GPIO_BCM=27
UDAR_PULL_UP=false
UDAR_MEASUREMENT_MODE=pulse
UDAR_PULSE_EDGE=rising
UDAR_POLL_INTERVAL_SECONDS=0.001
UDAR_BOUNCE_SECONDS=0.001
UDAR_DAILY_RESET=true

UDAR_STATION_CODE=ABAKAN2
UDAR_NOTE="ABAKAN2 vurus sayimi"
UDAR_HTTP_TIMEOUT=5
UDAR_QUEUE_DB=/var/lib/udar-pi-agent/machine_events.sqlite3
```

Boşta `1`, vuruş anında `0` görüyorsan:

```env
UDAR_PULSE_EDGE=falling
```

Ne olduğunu test ederken geçici olarak:

```env
UDAR_PULSE_EDGE=both
```

## Süre Bazlı Makine İçin Örnek Ayar

Makine çalışırken voltaj sürekli `1`, durunca `0` ise:

```env
UDAR_CRM_URL=https://crm.aykadoor.com
UDAR_DEVICE_TOKEN=CRMDEKI_CIHAZ_TOKENI

UDAR_GPIO_BCM=27
UDAR_PULL_UP=false
UDAR_MEASUREMENT_MODE=duration
UDAR_DURATION_UNIT=seconds
UDAR_MIN_DURATION_SECONDS=0.2
UDAR_POLL_INTERVAL_SECONDS=0.002
UDAR_BOUNCE_SECONDS=0.005
UDAR_DAILY_RESET=true

UDAR_STATION_CODE=LZR-1
UDAR_NOTE="LZR-1 calisma suresi"
UDAR_HTTP_TIMEOUT=5
UDAR_QUEUE_DB=/var/lib/udar-pi-agent/machine_events.sqlite3
```

## GPIO Testi

CRM'ye göndermeden sadece pinin okunup okunmadığını görmek için:

```bash
sudo systemctl stop udar-pi-agent
python3 ~/udar-pi-agent/diagnose_gpio.py --pin 27 --edge both --poll 0.001 --debounce 0.001
```

Çalışan pin fiziksel pin 11 ise:

```bash
python3 ~/udar-pi-agent/diagnose_gpio.py --pin 17 --edge both --poll 0.001 --debounce 0.001
```

Test bitince:

```bash
sudo systemctl restart udar-pi-agent
```

## Fiziksel Bağlantı

Varsayılan bağlantı:

- Raspberry Pi fiziksel pin 11: sinyal girişi, BCM/GPIO17.
- Raspberry Pi fiziksel pin 9: GND / Terra.
- Makineden gelen sinyal GPIO'ya 3.3V logic olarak gelmelidir.
- AYKA testinde çalışan alternatif bağlantı: fiziksel pin 13, BCM/GPIO27. Bu durumda `UDAR_GPIO_BCM=27` yazılır.

```text
Makine 3.3V pulse/sinyal çıkışı -> Raspberry Pi fiziksel pin 11 (GPIO17)
Makine GND / Terra              -> Raspberry Pi fiziksel pin 9 (GND)
```

GPIO27 kullanılan bağlantı:

```text
Makine 3.3V pulse/sinyal çıkışı -> Raspberry Pi fiziksel pin 13 (GPIO27)
Makine GND / Terra              -> Raspberry Pi fiziksel pin 9 (GND)
```

Önemli:

- GPIO pinine 5V, 12V, 24V veya endüstriyel makine voltajı doğrudan bağlanmaz.
- Makineden gelen sinyal 3.3V değilse optokuplör, röle modülü veya seviye dönüştürücü kullan.
- Makine GND/Terra hattı ile Raspberry Pi GND ortak referans olmalıdır.

## CRM Tarafı

1. `İmalat Yönetimi > Cihaz & Veri` ekranında ilgili istasyona bir cihaz oluştur.
2. Cihaz tokenını kopyala.
3. Tablette ilgili istasyonda tek aktif iş/oturum varsa Pi `line_id` göndermek zorunda değildir; CRM aktif oturumdan iş emrini bulur.
4. Aynı istasyonda birden fazla aktif iş olabiliyorsa `UDAR_LINE_ID` ver veya CRM cihaz eşlemesinde `$.line_id -> line_id` mapping'i tanımla.

Önerilen cihaz eşlemeleri:

| Kaynak path | Hedef alan | Tip | Zorunlu |
| --- | --- | --- | --- |
| `$.counter.delta` | `quantity_delta` | number | hayır |
| `$.counter.total` | `counter_value` | number | hayır |
| `$.line_id` | `line_id` | number | hayır |
| `$.operator_id` | `operator_id` | text | hayır |

Ajan zaten `quantity_delta`, `counter_value`, `idempotency_key`, `gpio`, `timestamp`, `counter` alanlarını gönderir.

## Tek Komut Kurulum

Raspberry Pi OS Lite üzerinde, git kurulu olmasa bile tek satır:

```bash
wget -qO- https://raw.githubusercontent.com/Enes3078/udar-pi-agent/main/bootstrap.sh | bash
```

Eğer `wget` yoksa:

```bash
sudo apt-get update && sudo apt-get install -y wget && wget -qO- https://raw.githubusercontent.com/Enes3078/udar-pi-agent/main/bootstrap.sh | bash
```

Alternatif manuel kurulum:

```bash
cd ~
git clone https://github.com/Enes3078/udar-pi-agent.git
cd udar-pi-agent
chmod +x install.sh update.sh
bash install.sh
```

Script sana şunları sorar:

- CRM adresi
- cihaz tokeni
- GPIO pini
- makine tipi:
  - `1`: vuruş/sayım bazlı
  - `2`: süre/saniye bazlı
- istasyon kodu

Sonradan ayarı değiştirmek için:

```bash
cd ~/udar-pi-agent
bash install.sh --configure
sudo systemctl restart udar-pi-agent
```

Elle düzenlemek istersen:

```bash
sudo nano /etc/udar-pi-agent.env
```

```env
UDAR_CRM_URL=https://crm.aykadoor.com
UDAR_DEVICE_TOKEN=CRMDEKI_CIHAZ_TOKENI
UDAR_GPIO_BCM=27
```

Servisi başlat:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now udar-pi-agent
sudo systemctl status udar-pi-agent
journalctl -u udar-pi-agent -f
```

## Güncelleme

Yeni sürüm çıktığında USB kullanmadan cihazda sadece şunu çalıştır:

```bash
cd ~/udar-pi-agent
bash update.sh
```

Bu işlem:

- public repodan son kodu çeker,
- `/etc/udar-pi-agent.env` dosyasını korur,
- ajan dosyasını `/opt/udar-pi-agent` altına kopyalar,
- systemd servisini yeniden başlatır.

Güncelleme sırasında makine tipi sorulmaz. Makineyi vuruştan süreye veya süreden vuruşa çevirmek istersen ayrıca:

```bash
cd ~/udar-pi-agent
bash install.sh --configure
sudo systemctl restart udar-pi-agent
```

## Elle Bağımlılık Kurulumu

Kurulum scripti paketleri otomatik kurar. Gerekirse elle:

```bash
sudo apt install -y python3-gpiozero python3-requests python3-rpi.gpio sqlite3
```

## Pulse Modu

Butona/her basıma 1 işlem yazılacak makinelerde:

```env
UDAR_MEASUREMENT_MODE=pulse
UDAR_DAILY_RESET=true
UDAR_BOUNCE_SECONDS=0.05
```

GPIO17'ye her 3.3V geldiğinde ajan `quantity_delta=1` gönderir. `counter_value` o günün toplam sayısıdır.

Elle değiştireceğin ana satır:

```env
UDAR_MEASUREMENT_MODE=pulse
UDAR_PULSE_EDGE=rising
UDAR_POLL_INTERVAL_SECONDS=0.002
UDAR_BOUNCE_SECONDS=0.005
UDAR_NOTE=GPIO27 vurus sayimi
```

Eğer makine boşta `1`, vuruşta `0` veriyorsa:

```env
UDAR_PULSE_EDGE=falling
```

Ne olduğundan emin değilsen geçici test için:

```env
UDAR_PULSE_EDGE=both
```

GPIO'yu CRM/API olmadan test etmek için:

```bash
sudo systemctl stop udar-pi-agent
python3 ~/udar-pi-agent/diagnose_gpio.py --pin 27 --edge rising --poll 0.002 --debounce 0.005
```

Boşta `0`, vuruşta `1` ise `rising`; boşta `1`, vuruşta `0` ise `falling` kullan.

## Süre Modu

Voltaj aktif kaldığı süre iş olarak sayılacak makinelerde:

```env
UDAR_MEASUREMENT_MODE=duration
UDAR_DURATION_UNIT=seconds
UDAR_MIN_DURATION_SECONDS=0.2
UDAR_DAILY_RESET=true
```

GPIO yüksek olunca süre başlar, GPIO tekrar 0 olunca tek kayıt olarak toplam süre gönderilir. `quantity_delta` o çalışma periyodunun süresi, `counter_value` o günün toplam çalışma süresidir. CRM, bu süre aralığını tablet oturumlarına göre ilgili çalışanlara dağıtır; moladaki veya sonradan gelen çalışan önceki süreyi almaz.

Elle değiştireceğin ana satırlar:

```env
UDAR_MEASUREMENT_MODE=duration
UDAR_DURATION_UNIT=seconds
UDAR_MIN_DURATION_SECONDS=0.2
UDAR_NOTE=GPIO27 sure olcumu
```

## Yerel Log ve Kuyruk

Son gönderimleri görmek:

```bash
sqlite3 /var/lib/udar-pi-agent/machine_events.sqlite3 \
  "select id,status,response_code,created_at,sent_at from event_log order by id desc limit 20;"
```

Gönderilemeyen kuyruk:

```bash
sqlite3 /var/lib/udar-pi-agent/machine_events.sqlite3 \
  "select id,idempotency_key,attempts,last_error,created_at from events order by id;"
```

## Manuel API Testi

Pulse örneği:

```bash
curl -X POST "https://crm.udarsoft.com/api/production/pi/events/" \
  -H "Content-Type: application/json" \
  -d '{
    "token": "CRMDEKI_CIHAZ_TOKENI",
    "event_type": "quantity",
    "quantity_delta": 1,
    "counter_value": 1,
    "counter": {"total": 1, "delta": 1, "mode": "pulse"},
    "gpio": {"bcm": 17, "physical_pin": 11, "gnd_physical_pin": 9},
    "timestamp": "2026-06-26T12:00:00Z",
    "idempotency_key": "manual-pulse-test-1"
  }'
```

Süre örneği:

```bash
curl -X POST "https://crm.udarsoft.com/api/production/pi/events/" \
  -H "Content-Type: application/json" \
  -d '{
    "token": "CRMDEKI_CIHAZ_TOKENI",
    "event_type": "quantity",
    "quantity_delta": 12.4,
    "counter_value": 58.7,
    "counter": {"total": 58.7, "delta": 12.4, "mode": "duration", "unit": "seconds"},
    "duration": {"seconds": 12.4},
    "gpio": {"bcm": 17, "physical_pin": 11, "gnd_physical_pin": 9},
    "timestamp": "2026-06-26T12:00:00Z",
    "idempotency_key": "manual-duration-test-1"
  }'
```

Eğer tablette ilgili istasyonda tek aktif iş yoksa payload'a `line_id` ekle:

```json
{"line_id": 123}
```

## Cloudflared Gerekir mi?

Pi, `https://crm.udarsoft.com` veya kullanılacak CRM alan adına doğrudan çıkabiliyorsa Cloudflared gerekmez. CRM yalnız iç ağda kalacaksa veya Pi doğrudan erişemeyecekse Cloudflared/Tailscale/WireGuard gibi bir tünel kurulabilir. Bu ajan için en temiz yapı Pi'nin HTTPS üzerinden CRM API'ye outbound istek atmasıdır.
