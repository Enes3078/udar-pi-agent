# UDAR CRM Raspberry Pi Makine Ajanı

Bu ajan, makineden Raspberry Pi'a gelen 3.3V sinyali okuyup UDAR CRM üretim API'sine gönderir. İki çalışma tipi vardır:

- `pulse`: GPIO17'ye her 3.3V gelişini 1 işlem olarak sayar.
- `duration`: GPIO17 yüksek kaldığı süreyi ölçer ve süreyi üretim verisi olarak gönderir.

Her iki modda da günlük sayaç gece yeni güne geçince sıfırlanır, fakat olay geçmişi yerel SQLite logunda saklanır. İnternet veya CRM kesilirse veriler kuyruğa alınır ve tekrar gönderilir.

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

Raspberry Pi OS üzerinde:

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
