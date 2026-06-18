import json, signal, sys, logging, subprocess, threading, time
import paho.mqtt.client as mqtt

try:
    import RPi.GPIO as GPIO
    ON_PI = True
except ImportError:
    ON_PI = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger()

# ── Config ────────────────────────────────────────────────
MQTT_HOST      = "5b5d1fc229d14745991d15a955fef7ca.s1.eu.hivemq.cloud"
MQTT_PORT      = 8883
MQTT_USER      = "spyrobot"
MQTT_PASS      = "MySecurePass123"
MQTT_CLIENT_ID = "spyrobot-pi-001"

MEDIAMTX_HOST  = "192.168.1.12"   # laptop IP running MediaMTX

# GPIO pins (BCM) — L298N
L_FWD, L_BWD, L_PWM = 16, 16, 20
R_FWD, R_BWD, R_PWM = 13, 13, 26

pwm_left = pwm_right = None
speed_pct = 60.0

# ── LiDAR (Benewake TF-Luna / TFmini over UART) ───────────
LIDAR_PORT       = "/dev/serial0"   # UART; check `ls -l /dev/serial0`
LIDAR_BAUD       = 115200
OBSTACLE_STOP_CM = 25               # block forward motion below this distance
OBSTACLE_WARN_CM = 60               # dashboard turns the readout amber below this
TELEMETRY_HZ     = 5                # lidar publish rate to the dashboard

lidar          = None
last_front_cm  = -1                 # -1 = no valid reading yet
_telemetry_run = True

# ── GPIO ──────────────────────────────────────────────────
def setup_gpio():
    global pwm_left, pwm_right
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    for pin in [L_FWD, L_BWD, L_PWM, R_FWD, R_BWD, R_PWM]:
        GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)
    pwm_left  = GPIO.PWM(L_PWM, 1000)
    pwm_right = GPIO.PWM(R_PWM, 1000)
    pwm_left.start(0)
    pwm_right.start(0)
    log.info("GPIO ready")

def _drive(speed, fwd, bwd, pwm):
    GPIO.output(fwd, GPIO.HIGH if speed > 0 else GPIO.LOW)
    GPIO.output(bwd, GPIO.HIGH if speed < 0 else GPIO.LOW)
    pwm.ChangeDutyCycle(min(abs(speed), 100))

def set_motors(left, right):
    if ON_PI:
        _drive(left,  L_FWD, L_BWD, pwm_left)
        _drive(right, R_FWD, R_BWD, pwm_right)
    else:
        log.info(f"[SIM] L={left:+.0f}  R={right:+.0f}")

def stop_motors():
    set_motors(0, 0)

def xy_to_motors(x, y):
    scale = speed_pct / 100.0
    left  = (y + x) * 100 * scale
    right = (y - x) * 100 * scale
    peak  = max(abs(left), abs(right), 1)
    if peak > 100:
        left  *= 100 / peak
        right *= 100 / peak
    return left, right



# ── LiDAR: Benewake TF-Luna / TFmini (single-beam, UART) ──
class LidarReader:
    """
    Reads the Benewake TF-Luna / TFmini(-Plus) 9-byte UART frame:
        0x59 0x59 Dist_L Dist_H Str_L Str_H Temp_L Temp_H Checksum
    Distance is returned directly in centimetres (0–800 cm).

    Wiring (TF-Luna in default UART mode):
        5V  -> Pi 5V (pin 2/4)      GND -> Pi GND (pin 6)
        RXD -> Pi TXD/GPIO14 (pin 8)  TXD -> Pi RXD/GPIO15 (pin 10)
    Enable UART once: sudo raspi-config -> Interface -> Serial
        login-shell = No, serial-hardware = Yes, then reboot.

    Single-beam sensor: only `front_cm` is real; left/right are None.
    """

    def __init__(self, port=LIDAR_PORT, baud=LIDAR_BAUD):
        self.port, self.baud = port, baud
        self._ser = None
        if ON_PI:
            try:
                import serial
                self._ser = serial.Serial(port, baud, timeout=0.2)
                log.info(f"LIDAR: TF-Luna open on {port} @ {baud}")
            except Exception as e:
                log.warning(f"LIDAR: serial open failed ({e}) — simulating")
        else:
            log.info("LIDAR: simulation mode (not on Pi)")

    def _read_frame(self):
        """Sync to a header and return one distance (cm) or None."""
        ser = self._ser
        for _ in range(18):                       # bounded header hunt
            if ser.read(1) != b"\x59":
                continue
            if ser.read(1) != b"\x59":
                continue
            body = ser.read(7)
            if len(body) != 7:
                return None
            if (0x59 + 0x59 + sum(body[:6])) & 0xFF != body[6]:
                return None                       # bad checksum, drop frame
            return body[0] | (body[1] << 8)
        return None

    def read(self):
        if self._ser:
            try:
                self._ser.reset_input_buffer()    # always read the freshest frame
                dist = self._read_frame()
                if dist is not None:
                    return {"front_cm": dist, "left_cm": None, "right_cm": None}
            except Exception as e:
                log.warning(f"LIDAR read error: {e}")
            return {"front_cm": last_front_cm, "left_cm": None, "right_cm": None}
        # simulation
        import random
        return {"front_cm": random.randint(20, 300), "left_cm": None, "right_cm": None}

    def close(self):
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass


def telemetry_loop(client):
    """Publish lidar distance + obstacle flag to the dashboard."""
    global last_front_cm
    period = 1.0 / TELEMETRY_HZ
    while _telemetry_run:
        try:
            r = lidar.read() if lidar else {"front_cm": -1, "left_cm": None, "right_cm": None}
            front = r.get("front_cm", -1)
            last_front_cm = front if front is not None else -1
            client.publish("robot/telemetry/lidar", json.dumps({
                "front_cm": front,
                "left_cm":  r.get("left_cm"),
                "right_cm": r.get("right_cm"),
                "obstacle": 0 <= last_front_cm < OBSTACLE_WARN_CM,
                "warn_cm":  OBSTACLE_WARN_CM,
                "stop_cm":  OBSTACLE_STOP_CM,
            }), qos=0)
        except Exception as e:
            log.warning(f"Telemetry error: {e}")
        time.sleep(period)


# ── Audio is handled entirely by go2rtc (speaker_in stream). ──
# These remain safe no-ops so the command hooks / shutdown path never raise.
def start_audio():
    pass

def stop_audio():
    pass


# ── MQTT callbacks ────────────────────────────────────────
def on_connect(client, userdata, flags, reason_code, properties=None):
    ok = (reason_code == 0) if isinstance(reason_code, int) else (not reason_code.is_failure)
    if ok:
        log.info("MQTT connected")
        client.subscribe([
            ("robot/cmd/move",   1),
            ("robot/cmd/stop",   1),
            ("robot/cmd/speed",  1),
            ("robot/cmd/camera", 1),
          #  ("robot/cmd/audio",  1),
        ])
    else:
        log.error(f"Connect failed: {reason_code}")

def on_message(client, userdata, msg):
    global speed_pct
    log.info(f"RX {msg.topic}  {msg.payload.decode()}")

    try:
        data = json.loads(msg.payload)
    except Exception:
        return

    if msg.topic == "robot/cmd/move":
        x = float(data.get("x", 0))
        y = float(data.get("y", 0))
        # Obstacle safety: block forward motion when something is too close.
        # (y > 0 = forward; last_front_cm == -1 means "no reading", so don't block.)
        if y > 0 and 0 <= last_front_cm < OBSTACLE_STOP_CM:
            log.warning(f"Obstacle {last_front_cm}cm — forward blocked")
            y = 0
        set_motors(*xy_to_motors(x, y))

    elif msg.topic == "robot/cmd/stop":
        stop_motors()

    elif msg.topic == "robot/cmd/speed":
        speed_pct = float(data.get("speed", 60))
        log.info(f"Speed → {speed_pct}%")

    elif msg.topic == "robot/cmd/camera":
        log.info(f"Camera pan={data.get('pan', 0)} tilt={data.get('tilt', 0)}")

    elif msg.topic == "robot/cmd/audio":
        # {"enabled": true}  or  {"enabled": false}
        if data.get("enabled", False):
            start_audio()
        else:
            stop_audio()

def on_disconnect(client, userdata, reason_code, properties=None):
    log.warning(f"Disconnected: {reason_code}")
    stop_motors()

# ── Main ──────────────────────────────────────────────────
def main():
    global lidar
    if ON_PI:
        setup_gpio()

    # Initialise the LiDAR (falls back to simulation off-Pi or on error)
    lidar = LidarReader()

    try:
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=MQTT_CLIENT_ID,
            protocol=mqtt.MQTTv5,
        )
    except (AttributeError, TypeError):
        client = mqtt.Client(client_id=MQTT_CLIENT_ID, protocol=mqtt.MQTTv5)

    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.tls_set()
    client.on_connect    = on_connect
    client.on_message    = on_message
    client.on_disconnect = on_disconnect

    client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)

    # Publish lidar telemetry to the dashboard in the background
    threading.Thread(target=telemetry_loop, args=(client,), daemon=True).start()

    def shutdown(sig, frame):
        global _telemetry_run
        _telemetry_run = False
        stop_motors()
        stop_audio()
        if lidar:
            lidar.close()
        client.disconnect()
        if ON_PI:
            pwm_left.stop()
            pwm_right.stop()
            GPIO.cleanup()
        sys.exit(0)

    signal.signal(signal.SIGINT,  shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    log.info("Waiting for commands…")
    client.loop_forever()

if __name__ == "__main__":
    main()
