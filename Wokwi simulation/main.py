import network, time, json, math, random, socket
from machine import Pin, I2C, ADC, PWM, UART
from umqtt_simple import MQTTClient
import dht

USE_REAL_GPS = False     # set True on real hardware (UART1: TX=GP4, RX=GP5)

WIFI_SSID, WIFI_PASS = "Wokwi-GUEST", ""
MQTT_HOST = "broker.hivemq.com"
TOPIC_TELE = b"helmet/h01/telemetry"
TOPIC_ALERT = b"helmet/h01/alert"

# Thresholds
TEMP_LIMIT, TEMP_RESET = 45.0, 43.0     # high temp alert above 45 C, clears below 43 C
O2_LIMIT, O2_RESET = 90.0, 92.0         # low oxygen (gas leak) alert below 90 %, clears at 92 % or more
NOISE_LIMIT, NOISE_RESET = 85.0, 80.0   # dB, 85 dB is the usual 8-hour exposure limit
NOISE_BEEP = 1000                       # ms, noise only gives one short beep
FALL_IMPACT_G = 1.8                     # impact spike in g (Wokwi slider max is 2 g, use 2.5-3 on real hardware)
STILL_TOL = 0.15                        # |g - 1| below this means no movement
STILL_TIME = 1500                       # ms of stillness after the impact
CANCEL_WINDOW = 5000                    # ms to cancel the fall alert with the button
WEAR_TIMEOUT = 300000                   # تم زيادة الوقت إلى 5 دقائق حتى لا تفصل الخوذة أثناء الإنذارات أو الثبات
BASE_LAT, BASE_LON = 30.0000, 31.0000   # simulated GPS start point, change it

i2c = I2C(0, sda=Pin(0), scl=Pin(1), freq=400000)
devices = i2c.scan()
MPU = 0x68
LCD_ADDR = 0x27 if 0x27 in devices else 0x3F

sensor_dht = dht.DHT22(Pin(2))
btn = Pin(16, Pin.IN, Pin.PULL_UP)
pir = Pin(14, Pin.IN)
noise_adc = ADC(Pin(27))    # potentiometer simulates the noise sensor
gas_adc = ADC(Pin(28))      # gas sensor AOUT, used as the oxygen sensor
uart = UART(1, baudrate=9600, tx=Pin(4), rx=Pin(5), timeout=10) if USE_REAL_GPS else None

bz = PWM(Pin(15))
bz.freq(2000)
bz.duty_u16(0)

def buzz(on):
    bz.duty_u16(32768 if on else 0)

i2c.writeto_mem(MPU, 0x6B, b'\x00')
i2c.writeto_mem(MPU, 0x1C, b'\x10')    # accelerometer range +-8g
ACC_SCALE = (16384, 8192, 4096, 2048)[(i2c.readfrom_mem(MPU, 0x1C, 1)[0] >> 3) & 3]

class LCD:
    def __init__(self, i2c, addr):
        self.i2c, self.addr, self.bl = i2c, addr, 0x08
        time.sleep_ms(50)
        for c in (0x33, 0x32, 0x28, 0x0C, 0x06, 0x01):
            self._send(c, 0)
            time.sleep_ms(5)

    def _pulse(self, d):
        self.i2c.writeto(self.addr, bytes([d | self.bl | 0x04]))
        self.i2c.writeto(self.addr, bytes([(d | self.bl) & ~0x04]))

    def _send(self, v, mode):
        self._pulse((v & 0xF0) | mode)
        self._pulse(((v << 4) & 0xF0) | mode)

    def clear(self):
        self._send(0x01, 0)
        time.sleep_ms(3)

    def put(self, row, text):
        self._send(0x80 | (0x40 if row else 0), 0)
        for ch in "{:<16}".format(text)[:16]:
            self._send(ord(ch), 1)

lcd = LCD(i2c, LCD_ADDR)

worn = False
last_pir = None
temp_alarm = temp_silenced = gas_alarm = gas_silenced = noise_alarm = False
noise_beep_end = 0
fall_state = 0   # 0 idle, 1 wait for stillness, 2 cancel countdown, 3 alert sent
impact_t = still_t = fall_t = 0
temp, o2, noise = 25.0, 100.0, 40.0
lat, lon = BASE_LAT, BASE_LON
last_tele = last_lcd = last_gps = 0
pending = []

def wifi_connect():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(WIFI_SSID, WIFI_PASS)
    lcd.put(0, "Connecting WiFi")
    t0 = time.ticks_ms()
    while not wlan.isconnected():
        if time.ticks_diff(time.ticks_ms(), t0) > 20000:
            print("WiFi timeout, retrying")
            wlan.connect(WIFI_SSID, WIFI_PASS)
            t0 = time.ticks_ms()
        time.sleep_ms(300)
    print("WiFi OK:", wlan.ifconfig())

client = None
last_mqtt_try = None

def mqtt_connect():
    global client, last_mqtt_try
    now = time.ticks_ms()
    if last_mqtt_try is not None and time.ticks_diff(now, last_mqtt_try) < 15000:
        return   # retry the broker at most every 15 s so the helmet loop never blocks
    last_mqtt_try = now
    try:
        s = socket.socket()
        s.settimeout(5)
        s.connect(socket.getaddrinfo(MQTT_HOST, 1883)[0][-1])
        s.close()
        c = MQTTClient("helmet01-" + str(time.ticks_ms()), MQTT_HOST)
        c.connect()
        client = c
        print("MQTT connected")
    except Exception as e:
        print("MQTT connect error:", e)
        client = None

def publish(topic, payload):
    global client
    try:
        if client is None:
            mqtt_connect()
        if client is None:
            return False
        client.publish(topic, json.dumps(payload))
        return True
    except Exception as e:
        print("MQTT publish error:", e)
        client = None
        return False

def publish_alert(kind):
    print("ALERT:", kind)
    pending.append({"type": kind, "temp": temp, "o2": o2, "noise": noise,
                    "lat": round(lat, 6), "lon": round(lon, 6)})

def flush_alerts():
    while pending:
        if publish(TOPIC_ALERT, pending[0]):
            pending.pop(0)
        else:
            break

def publish_tele():
    alarms = [n for n, a in (("TEMP", temp_alarm), ("GAS", gas_alarm), ("NOISE", noise_alarm), ("FALL", fall_state >= 2)) if a]
    print("T:{:.1f}C O2:{:.0f}% Noise:{:.0f}dB Worn:{} GPS:{:.5f},{:.5f} Alarms:{}".format(
        temp, o2, noise, int(worn), lat, lon, ",".join(alarms) or "none"))
    publish(TOPIC_TELE, {"temp": temp, "o2": o2, "noise": noise,
                         "lat": round(lat, 6), "lon": round(lon, 6),
                         "worn": 1 if worn else 0,
                         "fall": 1 if fall_state >= 2 else 0})

def read_acc_g():
    d = i2c.readfrom_mem(MPU, 0x3B, 6)
    def s16(h, l):
        v = (h << 8) | l
        return v - 65536 if v > 32767 else v
    x = s16(d[0], d[1]) / ACC_SCALE
    y = s16(d[2], d[3]) / ACC_SCALE
    z = s16(d[4], d[5]) / ACC_SCALE
    return math.sqrt(x * x + y * y + z * z)

def update_gps():
    global lat, lon, last_gps
    if USE_REAL_GPS:
        while uart.any():
            line = uart.readline()
            if not line:
                break
            try:
                s = line.decode()
            except Exception:
                continue
            if s[3:6] == "GGA":
                p = s.split(",")
                if len(p) > 6 and p[6] not in ("", "0") and p[2] and p[4]:
                    la, lo = float(p[2]), float(p[4])
                    lat = int(la / 100) + (la % 100) / 60
                    lon = int(lo / 100) + (lo % 100) / 60
                    if p[3] == "S": lat = -lat
                    if p[5] == "W": lon = -lon
    else:
        now = time.ticks_ms()
        if time.ticks_diff(now, last_gps) > 2000:
            last_gps = now
            lat += (random.random() - 0.5) * 0.00006
            lon += (random.random() - 0.5) * 0.00006

def check_wear():
    global worn, last_pir, temp_alarm, gas_alarm, noise_alarm, fall_state
    now = time.ticks_ms()
    if pir.value():
        last_pir = now
    now_worn = last_pir is not None and time.ticks_diff(now, last_pir) < WEAR_TIMEOUT
    if now_worn and not worn:
        worn = True
        lcd.clear()
        lcd.put(0, "Helmet ON")
        lcd.put(1, "System started")
        time.sleep_ms(1500)
    elif not now_worn and worn:
        worn = False
        temp_alarm = gas_alarm = noise_alarm = False
        fall_state = 0
        buzz(False)
        lcd.clear()
        lcd.put(0, "Helmet OFF")
        lcd.put(1, "System standby")

def check_temp(ack):
    global temp, temp_alarm, temp_silenced
    try:
        sensor_dht.measure()
        temp = sensor_dht.temperature()
    except OSError:
        pass
    if temp > TEMP_LIMIT and not temp_alarm:
        temp_alarm, temp_silenced = True, False
        publish_alert("high_temp")
    if temp_alarm and temp < TEMP_RESET:
        temp_alarm = temp_silenced = False
    if temp_alarm and ack:
        temp_silenced = True

def check_gas(ack):
    global o2, gas_alarm, gas_silenced
    gas_pct = gas_adc.read_u16() / 65535 * 100
    o2 = round(100 - gas_pct * 0.3, 1)   # simulation: more gas = less oxygen (100 % gas reading = 70 % O2)
    if o2 < O2_LIMIT and not gas_alarm:
        gas_alarm, gas_silenced = True, False
        publish_alert("gas")
    elif gas_alarm and o2 >= O2_RESET:
        gas_alarm = gas_silenced = False
    if gas_alarm and ack:
        gas_silenced = True

def check_noise():
    global noise, noise_alarm, noise_beep_end
    noise = round(40 + noise_adc.read_u16() / 65535 * 80, 1)   # potentiometer maps to 40-120 dB
    if noise > NOISE_LIMIT and not noise_alarm:
        noise_alarm = True
        noise_beep_end = time.ticks_add(time.ticks_ms(), NOISE_BEEP)
    elif noise_alarm and noise < NOISE_RESET:
        noise_alarm = False

def check_fall(ack):
    global fall_state, impact_t, still_t, fall_t
    g = read_acc_g()
    now = time.ticks_ms()
    if fall_state == 0:
        if g > FALL_IMPACT_G:
            fall_state, impact_t, still_t = 1, now, None
    elif fall_state == 1:
        if abs(g - 1.0) < STILL_TOL:
            if still_t is None:
                still_t = now
            if time.ticks_diff(now, still_t) >= STILL_TIME:
                fall_state, fall_t = 2, now
        else:
            still_t = None
        if time.ticks_diff(now, impact_t) > 8000:
            fall_state = 0
    elif fall_state == 2:
        if ack:
            fall_state = 0
        elif time.ticks_diff(now, fall_t) >= CANCEL_WINDOW:
            publish_alert("fall")
            fall_state = 3
    elif fall_state == 3:
        if ack:
            fall_state = 0

def update_outputs():
    global last_lcd
    now = time.ticks_ms()
    beep = time.ticks_diff(noise_beep_end, now) > 0
    buzz((temp_alarm and not temp_silenced) or (gas_alarm and not gas_silenced)
         or beep or fall_state >= 2)
    if time.ticks_diff(now, last_lcd) < 500:
        return
    last_lcd = now
    l0 = "T:{:.1f}C O2:{:.0f}%".format(temp, o2)
    if fall_state == 2:
        left = (CANCEL_WINDOW - time.ticks_diff(now, fall_t)) // 1000 + 1
        l1 = "FALL? BTN {}s".format(left)
    elif fall_state == 3:
        l1 = "FALL ALERT SENT"
    elif temp_alarm:
        l1 = "HIGH TEMPERATURE"
    elif gas_alarm:
        l1 = "GAS LEAK! LOW O2"
    elif noise_alarm:
        l1 = "HIGH NOISE {:.0f}dB".format(noise)
    else:
        l1 = "Noise:{:.0f}dB OK".format(noise)
    lcd.put(0, l0)
    lcd.put(1, l1)

wifi_connect()
lcd.put(0, "Connecting MQTT")
mqtt_connect()
lcd.clear()
lcd.put(0, "Put helmet on...")

while True:
    check_wear()
    if not worn:
        flush_alerts()
        time.sleep_ms(100)
        continue
    ack = (btn.value() == 0)
    update_gps()
    check_temp(ack)
    check_gas(ack)
    check_noise()
    check_fall(ack)
    update_outputs()
    flush_alerts()
    if time.ticks_diff(time.ticks_ms(), last_tele) > 2000:
        last_tele = time.ticks_ms()
        publish_tele()
    time.sleep_ms(50)