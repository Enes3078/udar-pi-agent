# UDAR CRM Raspberry Pi Makine Ajanı

> **Sahaya kurulum GitHub'daki `Enes3078/udar-pi-agent` reposundan yapılır**
> (`bootstrap.sh` / `update.sh`). Bu dizindeki kod, test ve kurulum betikleri
> o repoyla **birebir aynı** tutulur; değişiklik önce burada yapılıp testleri
> geçince oraya gönderilir. Aşağısı CRM tarafının anlamını anlatır; kurulum
> adımları için GitHub reposunun README'sine bakın.
>
> 2026-09 dersi: bu iki kopya bir kez ayrıştı ve 6 Ağustos'taki gönderim
> düzeltmesi GitHub'a hiç gitmedi — `update.sh` çalıştıran Pi eski sürüme
> dönüyordu. Eşitlik denetimi:
> `diff <(git show HEAD:deploy/raspberry-pi/udar_pi_agent.py) <(curl -s https://raw.githubusercontent.com/Enes3078/udar-pi-agent/main/udar_pi_agent.py)`


Bu ajan, makineden Raspberry Pi'a gelen 3.3V sinyali okuyup UDAR CRM üretim API'sine gönderir. İki çalışma tipi vardır:

- `pulse`: GPIO17'deki her tam sinyal çevrimini (boş → 3.3V → boş) 1 işlem olarak sayar.
- `duration`: GPIO17 yüksek kaldığı süreyi ölçer ve süreyi üretim verisi olarak gönderir.

Her iki modda da günlük sayaç gece yeni güne geçince sıfırlanır, fakat olay geçmişi yerel SQLite logunda saklanır. İnternet veya CRM kesilirse veriler kuyruğa alınır ve tekrar gönderilir.

## Fiziksel Bağlantı

Bağlamadan önce aşağıdaki **Kablolama kontrol listesi**ni okuyun. Varsayılan bağlantı:

- Raspberry Pi fiziksel pin 11: sinyal girişi, BCM/GPIO17.
- Raspberry Pi fiziksel pin 9: GND / Terra.
- Makineden gelen sinyal GPIO'ya 3.3V logic olarak gelmelidir.
- Kurulum sihirbazının önerdiği bağlantı: fiziksel pin 13, BCM/GPIO27. Bu durumda `UDAR_GPIO_BCM=27` yazılır.

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
- Optokuplör yoksa makine GND/Terra hattı ile Raspberry Pi GND ortak referans olmalıdır. Optokuplör varsa ortak GND gerekmez (yalıtımın amacı budur).

## Kablolama kontrol listesi

Elektrikçi için. Sayım yanlışsa ya da makine dururken vuruş geliyorsa önce bu liste
baştan sona kontrol edilir. Sahte vuruşların çoğu yazılımdan değil kablodan gelir.

1. **Sinyal doğru yerden mi alınıyor?** Sinyal, makinenin "bir iş bitti" anlamına
   gelen çıkışından alınmalı: çevrim sonu çıkışı, parça sayım çıkışı ya da hareketin
   sonunu gören bir sensör. İkaz lambası, flaşör, pompa rölesi ya da "makine açık"
   lambası hattı OLMAZ; bunlar iş yapılmadan da yanıp söner.
2. **Gerilim.** Makine sinyali 24 V (ya da 12 V, 5 V) ise araya o gerilime uygun
   optokuplör (yalıtım) kartı konur. Pi'nin pinine 3,3 V'tan yüksek gerilim ASLA
   doğrudan bağlanmaz.
3. **Gerilimsiz röle kontağı kullanılıyorsa** (Pi'nin kendi iç direnci çok zayıftır,
   tek başına yetmez):
   - Kontak 3,3 V pini ile giriş pini arasındaysa: giriş ile GND arasına 10 kΩ direnç
     ve 100 nF kondansatör takılır. Ayar: `UDAR_PULL_UP=false`, `UDAR_PULSE_EDGE=rising`.
   - Kontak giriş pini ile GND arasındaysa: giriş ile 3,3 V arasına 4,7–10 kΩ direnç
     takılır. Ayar: `UDAR_PULL_UP=true`, `UDAR_PULSE_EDGE=falling`.
4. **Makine kapalıyken giriş boşta kalmamalı.** Deneme: servis durdurulur
   (`sudo systemctl stop udar-pi-agent`), makine kapatılır,
   `sudo python3 /opt/udar-pi-agent/diagnose_gpio.py --is-sayisi 0` 10 dakika
   çalıştırılır. Ekranda tek bir `SAYILDI` satırı bile görünmemeli. Görünüyorsa giriş
   boşta kalıyor (yüzüyor) ya da parazit alıyor: 3. ve 5. maddelere dönülür. Bu deneme
   gerçek iş ölçümüyle aynı koşuda yapılmaz ("Ayarı ölçerek bulmak").
5. **Kablo.** Blendajlı (ekranlı) ve olabildiğince kısa olmalı. Blendaj yalnız Pi
   tarafında GND'ye bağlanır. Motor, kaynak ve kontaktör kablolarıyla aynı kanaldan
   geçmez; kesişmesi gerekiyorsa dik açıyla kesişir.
6. **Bobinler.** Kontaktör ve valf bobinlerinde söndürücü (RC ya da diyot) takılı olmalı.
7. **GND.** Optokuplör varsa ortak GND gerekmez. Yoksa GND tek bir noktadan bağlanır.
8. **Pi'nin saati doğru olmalı.** `timedatectl` komutu
   `System clock synchronized: yes` göstermeli. Pi'de pilli saat yoktur, saati
   internetten alır (ayrıntı: "Saat" bölümü).
9. **Tek ajan çalışmalı.** `pgrep -af udar_pi_agent` tek satır vermeli; eski
   kurulumdan kalma ikinci bir servis ya da elle başlatılmış kopya olmamalı. Yeni
   sürüm ikinci kopyayı zaten başlatmaz; `update.sh` de fazla kopya görürse uyarır.
10. **Ajan sürümü güncel olmalı.** `bash update.sh` çalıştırılır ve bu dosyanın
    başındaki eşitlik denetimi yapılır. `update.sh` kurulu sürümü yazar; sürüm ajanın
    açılış satırında (`journalctl -u udar-pi-agent`) ve CRM'e giden her kayıtta
    (ölçen: `measured_by_version`, gönderen: `agent_version`) da görünür.

## CRM Tarafı

1. `İmalat Yönetimi > Cihaz & Veri` ekranında ilgili istasyona bir cihaz oluştur.
2. Cihaz tokenını kopyala.
3. Tablet ekranında seçili olan iş emri CRM tarafından istasyonun aktif RPi hedefi olarak kaydedilir. Aynı istasyonda çok sayıda açık iş emri olsa bile Pi sayımı tablette seçili işe yazılır.
4. `UDAR_LINE_ID` yalnız cihazın kalıcı olarak tek bir iş emri satırına sabitlenmesi gereken özel kurulumlarda kullanılır. Normal tablet kullanımında boş bırakılır.

Önerilen cihaz eşlemeleri:

| Kaynak path | Hedef alan | Tip | Zorunlu |
| --- | --- | --- | --- |
| `$.counter.delta` | `quantity_delta` | number | hayır |
| `$.counter.total` | `counter_value` | number | hayır |
| `$.line_id` | `line_id` | number | hayır |
| `$.operator_id` | `operator_id` | text | hayır |

Ajan zaten `quantity_delta`, `counter_value`, `idempotency_key`, `gpio`, `timestamp`, `counter` alanlarını gönderir. Tam liste: "Ajanın gönderdiği alanlar".

## Raspberry Pi Kurulum

Raspberry Pi OS üzerinde:

```bash
sudo apt update
sudo apt install -y python3-gpiozero python3-requests python3-rpi.gpio sqlite3

sudo mkdir -p /opt/udar-pi-agent /var/lib/udar-pi-agent
sudo cp udar_pi_agent.py /opt/udar-pi-agent/udar_pi_agent.py
sudo cp diagnose_gpio.py /opt/udar-pi-agent/diagnose_gpio.py
sudo cp udar-pi-agent.service /etc/systemd/system/udar-pi-agent.service
sudo cp udar-pi-agent.env.example /etc/udar-pi-agent.env
sudo nano /etc/udar-pi-agent.env
```

`/etc/udar-pi-agent.env` içinde en az şu alanları doldur:

```env
UDAR_CRM_URL=https://crm.udarsoft.com
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

## Pulse Modu

Her tam sinyal çevrimine 1 işlem yazılacak makinelerde. Öndeğerler:

```env
UDAR_MEASUREMENT_MODE=pulse
UDAR_PULSE_EDGE=rising
UDAR_PULSE_MIN_ACTIVE_SECONDS=0.02
UDAR_PULSE_REARM_SECONDS=0.20
UDAR_PULSE_MAX_ACTIVE_SECONDS=10.0
UDAR_PULSE_MIN_INTERVAL_SECONDS=0.20
UDAR_DAILY_RESET=true
```

Ajan kabul ettiği her çevrimde `quantity_delta=1` gönderir; `counter_value` o günün
toplam sayısıdır. Bir vuruş ancak şu sırayla sayılır:

1. pin en az `UDAR_PULSE_REARM_SECONDS` boyunca kararlı biçimde boş kalır,
2. en az `UDAR_PULSE_MIN_ACTIVE_SECONDS` boyunca kararlı biçimde aktif olur,
3. yine en az `UDAR_PULSE_REARM_SECONDS` boyunca boş kalır.

Aktiflik `UDAR_PULSE_MAX_ACTIVE_SECONDS`'tan uzun sürerse (takılı sinyal) sayılmaz.
Önceki vuruştan `UDAR_PULSE_MIN_INTERVAL_SECONDS`'tan daha kısa süre sonra biten
çevrim de sayılmaz. Açılışta aktif kalan pin ve kısa elektrik gürültüsü sayılmaz.
Öndeğerlerle kabul edilen en kısa çevrim yaklaşık 0,22 saniyedir; bu yüzden
öndeğerdeki `UDAR_PULSE_MIN_INTERVAL_SECONDS=0.20` fiilen ek bir koruma getirmez.
Asıl koruma, ayarların makineye göre **ölçülerek** seçilmesidir (aşağıda).

Öndeğerler bilerek tarafsızdır ve değiştirilmez: değişirlerse sahadaki her kurulumun
sayımı değişir.

`UDAR_PULSE_EDGE`:

- `rising`: boşta pin 0, vuruşta 1 (en yaygın).
- `falling`: boşta pin 1, vuruşta 0.
- `both`: eski bir ayardır ve `rising` ile **aynı** çalışır: tam çevrimi 1 adet sayar,
  her 0/1 değişimini ayrı saymaz. Belge bir dönem aksini söylüyordu, ama kod hiçbir
  zaman öyle saymadı. Kodu belgeye uydurmak, `both` ayarlı cihazların bir güncellemeyle
  sessizce çift saymaya başlaması demekti (bir çevrimde iki değişim var). Bu yüzden
  davranış korundu, belge düzeltildi. Sihirbaz artık `both` önermiyor; ajan eski ayarı
  kabul eder ve açılışta hatırlatır.

`UDAR_BOUNCE_SECONDS` artık okunmuyor. Temmuz 2026'dan (`391b844`) beri kullanılmıyor;
daha eski ajanda gpiozero `Button`'ın `bounce_time` değeriydi. `391b844` sayımı
yukarıdaki çevrim süzgecine taşıdı; o günden beri değer yalnız okunuyordu, pine
verilmiyordu, ama bozuk bir değer ajanı açılışta çökertebiliyordu. Eski ayar
dosyasında kalmışsa zararı yok, silinebilir; ajan açılışta hatırlatır. Süzgeç
yukarıdaki `UDAR_PULSE_*` ayarlarıdır. Sahada Temmuz 2026 öncesi bir ajan hâlâ
çalışıyorsa bu değer onun için geçerlidir; o ajanın vuruş kayıtlarında
(`counter.mode` = `pulse`) `pulse` alanı yoktur. Süre kayıtlarında `pulse` alanı hiçbir
sürümde yoktur; orada bu alanın yokluğu eski ajan demek değildir ("Kaydı hangi ajan
ölçtü?").

## Ayarı ölçerek bulmak (makine türüne göre değil)

Aynı türden iki makine bile farklı sinyal verebilir. Ayar, makinenin adına ya da
türüne göre değil, o makinenin gerçekte ürettiği sinyal ölçülerek seçilir.
`diagnose_gpio.py` ajanın süzgecini aynen kullanır (tek kaynak) ve CRM'e hiçbir şey
göndermez.

1. Servisi durdurun: `sudo systemctl stop udar-pi-agent`
2. **Makine kapalı denemesi (ayrı koşu).** Makine kapalıyken
   `sudo python3 /opt/udar-pi-agent/diagnose_gpio.py --is-sayisi 0` çalıştırıp birkaç
   dakika (kontrol listesi 4. madde: 10 dakika) bekleyin, Ctrl+C ile çıkın. Ekrana hiç
   `SAYILDI` satırı düşmemeli; özet "Ajan MEVCUT ayarla DOGRU sayiyor (0)" demeli ve
   ölçülen çevrim 0 olmalı. Değilse sorun ayarda değil kablodadır (kontrol listesi
   3-6. maddeler): ayar önerisine geçmeyin. Bu denemede ayar önerilmez.
3. **Gerçek iş ölçümü (yeni koşu).** Ölçümü yeniden başlatın:
   `sudo python3 /opt/udar-pi-agent/diagnose_gpio.py`
   (`sudo`, ayar dosyasını okuyabilsin diye; pin, pull-up, kenar ve süzgeç ayarları
   oradan alınır. Denemek için `--pin`, `--pull-up`/`--no-pull-up`, `--edge`,
   `--min-active`, `--rearm`, `--max-active`, `--min-interval` verilebilir.)
   Makine kapalı bekleme bu koşuya karışmasın; makineyi hemen en hızlı GERÇEK
   çalışma temposunda 20-30 iş yapacak şekilde çalıştırın (boşa değil, gerçek işle)
   ve **işleri sayın**. Ekranda her ham değişim, aktif süre ve ajanın o çevrimi sayıp
   saymayacağı (`AJAN SAYARDI` / `ajan saymazdı`) görünür.
4. Ctrl+C ile çıkın. Araç "makine kaç iş yaptı?" diye sorar; saydığınız sayıyı yazın
   (ya da baştan `--is-sayisi 25` verin). Ajanın doğru sayıp saymadığını ancak bu
   sayı söyler: araç, ajanın sayısı ölçümden farklı diye ajanı "hatalı" ilan etmez;
   aynı diye de "doğru" ilan etmez (ajan ile ölçüm aynı işleri birlikte kaçırabilir).
   Sayıyı girmezseniz özet "Kontrol icin is sayisini girin" der ve ayarları `#` ile
   kapalı verir.
5. Özette şunlar yazar: ölçülen gerçek çevrim; ayıklanan kısa kopma ve parazit
   (sayısı ve en uzunu); ajanın **mevcut** ve **önerilen** ayarla aynı kayıtta kaç
   sayacağı; aktif süre, bekleme ve iki çevrim arası (alt %10 ve ortanca); sonuç ve
   **önerilen ayarlar**.
6. Öneriler yalnız özet "AYNEN yapistirilabilir" diyorsa girilir; bunu yalnız gerçek iş
   sayısı girilmiş bir ölçümde der. Giriş: `bash install.sh --configure` ile (ya da
   `sudo nano /etc/udar-pi-agent.env`).
   Sihirbaz "en kısa gerçek çevrim" sorusunda özetteki değeri ister ve iki vuruş arası
   süreyi buna göre önerir. Özet "DOGRUDAN GIRMEYIN" diyorsa satırlar `#` ile kapalı
   gelir ve nedeni yazar (aşağıda).
7. Yeni ayarla ölçümü tekrarlayın: "Ajan MEVCUT ayarla sayardı" sayısı gerçekte
   yapılan iş sayısına eşit olmalı. Sonra servisi başlatın:
   `sudo systemctl start udar-pi-agent`.

Öneri tek bir ölçüme değil sağlam istatistiğe dayanır:

- Kopma ve parazit, değerlerin **dağılımına** bakılarak ayıklanır; tek bir değere
  bakılmaz. Ölçülen değerler kısa ve gerçek diye iki kümeye ayrılır. Ayrım ancak gerçek
  küme en az **5** değerse ve kısa kümenin en uzunu gerçek kümenin alt %10'undan en az
  **4 kat** kısaysa yapılır. Böylece tek bir duraklama ya da tek bir uzun basış bütün
  işleri "kopma" ya da "parazit" yapamaz.
- **Kısa kopma ayıklanır.** Çevrimin ortasında sinyal bir an kopuyorsa (röle ya da
  limit şalteri sıçraması, kontak çırpması) parçalar tek çevrim sayılır. Kopma sayılan
  boşluk: 0,5 sn'den kısa, kısa kümede ve çevrimin en uzun kesintisiz parçasından en az
  4 kat kısa (kopma, kestiği sinyalden kısadır). Hızlı makinede işler arası boşluk
  sinyalden uzundur; duraklamalar olsa da işler birleşmez. Öndeğerli ajan 0,2 sn'den
  kısa kopmayı zaten yutar; öneri bunu bozmaz (bekleme ayarı hiçbir zaman en uzun
  kopmanın altına önerilmez).
- **Kısa parazit ayıklanır.** Aktif süresi 0,1 sn'den kısa, kısa kümede ve sayısı
  gerçek çevrimlerden **az** olan sinyal çevrim sayılmaz. Kısa darbeli sensörde birkaç
  uzun basış gerçek darbeleri parazit yapamaz. Parazit işten kalabalıksa hat bozuktur:
  ayıklanmaz, ölçüm gerçek iş sayısını tutmaz ve özet kabloyu gösterir.
- Kısa değerlerle gerçek değerler arasında böyle belirgin bir ayrım yoksa (ör. operatör
  temposuyla değişen süreler) hiçbir şey ayıklanmaz.
- Öneriler **alt %10**'dan hesaplanır: en kısa değerlerin %10'u, en az biri, dışarıda
  kalır. Tek aykırı ölçüm öneriyi belirleyemez.
- En az **10** gerçek çevrim yoksa öneri verilmez.
- Önerilen ayarla ajanın kaç sayacağı aynı kayıt üzerinde yeniden hesaplanır.

"AYNEN yapıştırılabilir" yalnız **gerçek iş sayısı verildiyse** ve önerilen ayarla sayım
ona eşitse denir. Süzgeci **gevşeten** öneri (ajanı daha çok saydırır) ayrıca yalnız
ajan mevcut ayarla gerçek işleri kaçırıyorsa girilir.

Gerçek iş sayısı verilmediyse hiçbir öneri yapıştırılmaz; özet "Kontrol icin is sayisini
girin" der. Ajanın, ölçümün ve önerinin aynı sayıyı bulması doğru saydıklarını
göstermez: örneğin 0,5 sn aktif, 0,1 sn boş, 0,5 sn aktif gelen iki gerçek vuruş kısa
kopmadan ayırt edilemez; o zaman ajan da ölçüm de öneri de yarı sayar. Bunu yalnız
gerçek iş sayısı ayırır.

Öneri kuralları (`UDAR_PULSE_MIN_INTERVAL_SECONDS` kuralı `install.sh` ile aynıdır):

| Ayar | Kural |
| --- | --- |
| `UDAR_PULSE_MIN_ACTIVE_SECONDS` | kesintisiz aktif sürenin alt %10'unun yarısı (ajan aktifliğin kesintisiz bu kadar sürmesini ister; kopmalı çevrimde en uzun kesintisiz parçaya bakılır). En uzun parazitten kısa kalırsa önerilmez |
| `UDAR_PULSE_REARM_SECONDS` | bekleme süresinin alt %10'unun yarısı, en çok 0,50. En uzun kopmadan kısa kalırsa önerilmez |
| `UDAR_PULSE_MAX_ACTIVE_SECONDS` | en uzun aktif sürenin 2 katı, en az 10 |
| `UDAR_PULSE_MIN_INTERVAL_SECONDS` | iki çevrim arasının alt %10'unun yarısı; 0,20'nin altına inilmez, yalnız çevrim 0,25 sn'den kısaysa yarısı |

Makinenin en kısa çevrimi 0,25 saniyeden kısaysa tek tek vuruş saymak hataya açıktır;
mümkünse makinenin kendi sayacı kullanılır.

Güncelleme öncesinde kuyruğa birikmiş sahte kayıtları yalnız gerektiğinde temizle:

```bash
sudo systemctl stop udar-pi-agent
sudo sqlite3 /var/lib/udar-pi-agent/machine_events.sqlite3 "DELETE FROM events;"
sudo systemctl start udar-pi-agent
```

## Süre Modu

Voltaj aktif kaldığı süre iş olarak sayılacak makinelerde:

```env
UDAR_MEASUREMENT_MODE=duration
UDAR_DURATION_UNIT=seconds
UDAR_MIN_DURATION_SECONDS=1.0
UDAR_DURATION_ACTIVE_LEVEL=high
UDAR_DURATION_START_STABLE_SECONDS=0.20
UDAR_DURATION_STOP_STABLE_SECONDS=0.20
UDAR_DURATION_DROPOUT_GRACE_SECONDS=1.50
UDAR_DURATION_DROPOUT_LOG_INTERVAL_SECONDS=30
UDAR_DAILY_RESET=true
```

GPIO yüksek olunca süre başlar. GPIO kısa süre 0'a düşüp
`UDAR_DURATION_DROPOUT_GRACE_SECONDS` dolmadan tekrar 1 olursa bu kopma
parazit kabul edilir ve çalışma bölünmez. Sinyal bu süre boyunca kesintisiz 0
kalırsa gerçek duruş kabul edilip tek kayıt olarak toplam süre gönderilir.
`quantity_delta` o çalışma periyodunun süresi, `counter_value` o günün toplam
çalışma süresidir. CRM, bu süre aralığını tablet oturumlarına göre ilgili
çalışanlara dağıtır; moladaki veya sonradan gelen çalışan önceki süreyi almaz.

Uzun süre kesintisiz çalışan makinelerde öndeğer `1.50` saniye uygundur.
Gerçek duruşların daha hızlı algılanması gerekiyorsa `0.75`, elektriksel
kopmalar daha uzunsa `2.00` denenebilir. Ajanın yok saydığı kopmalar
`journalctl` içinde 30 saniyede bir `duration input noise filtered` özeti
olarak görünür; her elektriksel kopma için ayrı log satırı üretilmez.

## Saat

Pi'de pilli saat yoktur. Açılışta saat, son kayıtlı değerden başlar ve internet gelip
eşitlenene kadar geride kalır. O arada ölçülen vuruşun zamanı yanlış olabilir.

- Servis `After=time-set.target` ile saatin kabaca kurulmasını bekler, ama internetten
  eşitlenmesini **beklemez**: internet yokken de vuruşlar sayılıp kuyruğa yazılmalı.
- Bu yüzden `systemd-time-wait-sync` servisini AÇMAYIN. Açılırsa ajan internet gelene
  kadar hiç başlamaz; o arada gelen vuruşlar sayılmaz, kuyruğa bile yazılmaz.
- Bunun yerine ajan saati kendisi izler ve her kayda iki bilgi ekler:
  `clock_synced` (ölçüm anında saat internetten eşitlenmiş miydi) ve
  `queue_age_seconds` (ölçümden gönderime geçen süre, saatin sıçramasından
  etkilenmeyen iç sayaçla). CRM ölçüm zamanını bunlarla düzeltebilir.
- Kontrol: `timedatectl` → `System clock synchronized: yes`. Eşitlenmemişse ajanın
  günlüğünde (`journalctl -u udar-pi-agent`) bir kez uyarı görünür.

## Tek ajan kuralı

Aynı Pi'de iki ajan aynı makineyi okursa her vuruş iki kez sayılır. Ajan açılışta
kuyruk dosyasının yanındaki kilidi alır
(`/var/lib/udar-pi-agent/machine_events.sqlite3.lock`). Kilit başkasındaysa ikinci
kopya hiçbir şey okumadan, nedenini yazarak çıkar. Ajan durunca ya da çökünce kilit
kendiliğinden bırakılır; kilit dosyasını silmek gerekmez.

Kilit, bu sürümden önceki (kilitsiz) bir ajanı durduramaz. Eski kurulumdan kalma bir
kopya elle ya da başka bir servisle çalışıyorsa `pgrep -af udar_pi_agent` birden fazla
satır verir; `update.sh` bunu görünce uyarır.

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

Ajan, gönderim hatalarında aynı kaydı sürekli göndermek yerine geri çekilerek
yeniden dener. Ağ hataları `UDAR_SEND_RETRY_BASE_SECONDS` ile başlar; 400/403
gibi CRM doğrulama hataları `UDAR_SEND_RETRY_VALIDATION_SECONDS` ile başlar;
tüm beklemeler `UDAR_SEND_RETRY_MAX_SECONDS` ile sınırlanır. Böylece aktif iş
henüz seçilmemişken veya CRM geçici olarak meşgulken sunucuya gereksiz istek
yükü binmez.

## Ajanın gönderdiği alanlar

| Alan | Anlamı |
| --- | --- |
| `quantity_delta`, `counter_value` | bu kaydın miktarı (vuruşta 1, sürede saniye) ve günün toplamı |
| `counter` | `total`, `delta`, `daily_sequence` (günün kaçıncı kaydı), `mode`, `unit` |
| `counter_day` | sayacın günü (Pi saatiyle) |
| `timestamp` | ölçüm anı, Pi saatiyle (UTC) |
| `pulse` / `duration` | vuruşta `edge`, `active_seconds`; sürede başlangıç, bitiş, saniye ve süzülen kopmalar |
| `gpio`, `host`, `note`, `idempotency_key` | pin bilgisi, Pi'nin adı, not, kayıt anahtarı |
| `agent_version` | kaydı **gönderen** ajanın sürümü. Alan yoksa kaydı 2026-09-30 öncesi bir ajan gönderdi |
| `measured_by_version` | vuruşu **ölçen** ajanın sürümü; ölçüm anında kuyruğa yazılır. `null` = ölçüm, bu alanı bilmeyen (2026-09-30 öncesi) bir ajanın kuyruğundan kaldı ve güncellemeden sonra gönderildi |
| `clock_synced` | ölçüm anında Pi saati internetten eşitlenmiş miydi: `true` / `false`; `null` = bilinmiyor |
| `queued_at` | kaydın kuyruğa girdiği an (Pi saati) |
| `sent_at` | bu gönderimin anı (Pi saati). `CRM saati − sent_at` Pi saatinin sapmasını verir |
| `attempts` | bu gönderim kaçıncı deneme (ilk gönderimde 1) |
| `queue_age_seconds` | ölçümden bu gönderime geçen süre (sn), Pi saatinin sıçramasından etkilenmez. Pi arada yeniden başladıysa gönderilmez |

`agent_version`, `sent_at`, `attempts` ve `queue_age_seconds` her denemede yeniden
hesaplanır; kuyruktaki ölçüm (`measured_by_version` dahil) değişmez. Pi internetsizken
eski ajanın biriktirdiği kayıt `update.sh`'den sonra yeni ajanla gider: `agent_version`
yeni sürümü, `measured_by_version` ise `null` gösterir. "Sahada eski ajan mı saydı?"
sorusunun cevabı `measured_by_version`'dadır, `agent_version`'da değil. Aynı kaydın iki
kez yazılmasını `idempotency_key` önler, o değişmez.
Yeni alanların hiçbiri CRM'in olay zamanı için okuduğu adları (`occurred_at`,
`timestamp`, `time`, `created_at`) kullanmaz.

### Kaydı hangi ajan ölçtü?

CRM (cihaz ekranındaki "eski Pi yazılımı" uyarısı, sahte vuruş teşhisi) bu sırayla okur:

1. `measured_by_version` doluysa kaydı o sürüm ölçtü.
2. `measured_by_version` var ama `null` ise ölçüm 2026-09-30 öncesi bir ajanın
   kuyruğundan kaldı, güncellemeden sonra yeni ajanla gönderildi. Daha eski dönemi
   3. maddedeki şekil söyler.
3. `measured_by_version` da `agent_version` da yoksa kaydı 2026-09-30 öncesi bir ajan
   ölçüp gönderdi. Daha eski dönemi ayırmak için ÖNCE `counter.mode`'a bakılır (her
   sürüm yazar):
   - `pulse`: `pulse` alanı yoksa Temmuz 2026 (`391b844`) öncesi `when_pressed`
     ajanıdır; onda `UDAR_BOUNCE_SECONDS` gpiozero `Button`'ın `bounce_time` değeriydi.
     `pulse` alanı varsa çevrim süzgeçli ajandır (`391b844` ve sonrası).
   - `duration`: `pulse` alanı hiçbir sürümde yazılmaz; yokluğu burada eski ajan demek
     DEĞİLDİR. `duration.validated_cycle` varsa 24 Temmuz 2026 (`4a90800`) ve sonrası
     süzgeçli süre ajanıdır; yoksa daha eski süre ajanıdır.
   - `counter.mode` ya da `host` yoksa kayıt ajandan gelmemiştir (ör. aşağıdaki elle
     deneme; ajan ikisini de her sürümde yazar); sınıflanmaz.

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

Tablet kullanılmayan özel bir kurulumda hedef iş payload ile sabitlenecekse `line_id` ekle:

```json
{"line_id": 123}
```

## Cloudflared Gerekir mi?

Pi, `https://crm.udarsoft.com` veya kullanılacak CRM alan adına doğrudan çıkabiliyorsa Cloudflared gerekmez. CRM yalnız iç ağda kalacaksa veya Pi doğrudan erişemeyecekse Cloudflared/Tailscale/WireGuard gibi bir tünel kurulabilir. Bu ajan için en temiz yapı Pi'nin HTTPS üzerinden CRM API'ye outbound istek atmasıdır.
