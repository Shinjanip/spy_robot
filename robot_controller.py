import json, signal, sys, logging, subprocess, threading, time, base64, queue
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

# ══════════════════════════════════════════════════════════
# MOTORS — Cytron MDD: ONE dir pin + ONE pwm pin per side.
# Each side's signals are shared by that side's two motors.
# ══════════════════════════════════════════════════════════
#   speed > 0 -> DIR set for forward, PWM = speed
#   speed < 0 -> DIR set for reverse, PWM = |speed|
#   speed = 0 -> PWM = 0 (stop)
#
#   LEFT  (both left motors):  DIR = 16, PWM = 20
#   RIGHT (both right motors): DIR = 13, PWM = 26
L_DIR, L_PWM = 20, 12     # was 16, 20
R_DIR, R_PWM = 22, 27     # was 13, 26
# ── Direction-fix knobs (set these by testing, no rewiring) ──
# After flashing, drive forward. If a side goes the WRONG way, flip its flag.
#   False = DIR HIGH is forward (default)
#   True  = DIR HIGH is reverse
L_REVERSED = False
R_REVERSED = False
# If pushing forward makes it turn (one side swapped), set True to swap sides.
SWAP_SIDES = False

pwm_left = pwm_right = None
speed_pct = 60.0

# ── Audio (talk-back via MQTT) ────────────────────────────
SPEAKER_DEVICE = "plughw:CARD=MAX98357A,DEV=0"   # MAX98357A; confirm with `aplay -l`
VOICE_TOPIC    = "robot/cmd/voice"
_voice_q   = queue.Queue(maxsize=20)
_voice_run = True

# ── LiDAR ─────────────────────────────────────────────────
LIDAR_PORT       = "/dev/serial0"
LIDAR_BAUD       = 115200
OBSTACLE_STOP_CM = 25
OBSTACLE_WARN_CM = 60
TELEMETRY_HZ     = 5

lidar          = None
last_front_cm  = -1
_telemetry_run = True

# ── GPIO ──────────────────────────────────────────────────
def setup_gpio():
    global pwm_left, pwm_right
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    for pin in [L_DIR, L_PWM, R_DIR, R_PWM]:
        GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)
    pwm_left  = GPIO.PWM(L_PWM, 1000)
    pwm_right = GPIO.PWM(R_PWM, 1000)
    pwm_left.start(0)
    pwm_right.start(0)
    log.info("GPIO ready")

def _drive(speed, dir_pin, pwm, reversed_flag):
    # forward when speed >= 0, unless this side is flagged reversed.
    forward = (speed >= 0) ^ reversed_flag
    GPIO.output(dir_pin, GPIO.HIGH if forward else GPIO.LOW)
    pwm.ChangeDutyCycle(min(abs(speed), 100))

def set_motors(left, right):
    if SWAP_SIDES:
        left, right = right, left
    if ON_PI:
        _drive(left,  L_DIR, pwm_left,  L_REVERSED)
        _drive(right, R_DIR, pwm_right, R_REVERSED)
    else:
        log.info(f"[SIM] L={left:+.0f}  R={right:+.0f}")

def stop_motors():
    set_motors(0, 0)

def xy_to_motors(x, y):
    # x = turn (right +), y = forward (+). Arcade mix, clamped to ±100.
    scale = speed_pct / 100.0
    left  = (y + x) * 100 * scale
    right = (y - x) * 100 * scale
    left  = max(-100, min(100, left))
    right = max(-100, min(100, right))
    return left, right

def motor_self_test():
    # Publish anything to robot/cmd/test to run this. Watch which step moves.
    log.info("TEST left fwd");  set_motors(50, 0);  time.sleep(0.7)
    log.info("TEST left rev");  set_motors(-50, 0); time.sleep(0.7)
    stop_motors();                                  time.sleep(0.3)
    log.info("TEST right fwd"); set_motors(0, 50);  time.sleep(0.7)
    log.info("TEST right rev"); set_motors(0, -50); time.sleep(0.7)
    stop_motors()
    log.info("TEST done")


# ── Voice playback (MQTT walkie-talkie) ───────────────────
def _play_clip(audio_bytes):
    if not ON_PI:
        log.info(f"[SIM] would play voice clip ({len(audio_bytes)} bytes)")
        return
    try:
        proc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-i", "pipe:0", "-af", "aresample=async=1",
             "-f", "alsa", SPEAKER_DEVICE],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        _, err = proc.communicate(audio_bytes, timeout=30)
        if proc.returncode != 0:
            log.warning(f"voice ffmpeg exit {proc.returncode}: {err.decode(errors='ignore').strip()}")
    except FileNotFoundError:
        log.error("ffmpeg not found — install it: sudo apt install ffmpeg")
    except subprocess.TimeoutExpired:
        proc.kill()
        log.warning("voice clip playback timed out")
    except Exception as e:
        log.warning(f"voice playback error: {e}")

def voice_worker():
    while _voice_run:
        try:
            audio_bytes = _voice_q.get(timeout=0.5)
        except queue.Empty:
            continue
        _play_clip(audio_bytes)
        _voice_q.task_done()

def handle_voice(payload):
    try:
        data  = json.loads(payload)
        audio = base64.b64decode(data.get("audio", ""))
    except Exception as e:
        log.warning(f"bad voice payload: {e}")
        return
    if not audio:
        return
    try:
        _voice_q.put_nowait(audio)
        log.info(f"voice clip queued ({len(audio)} bytes)")
    except queue.Full:
        log.warning("voice queue full — dropping clip")


# ── LiDAR: Benewake TF-Luna / TFmini (single-beam, UART) ──
class LidarReader:
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
        ser = self._ser
        for _ in range(18):
            if ser.read(1) != b"\x59":
                continue
            if ser.read(1) != b"\x59":
                continue
            body = ser.read(7)
            if len(body) != 7:
                return None
            if (0x59 + 0x59 + sum(body[:6])) & 0xFF != body[6]:
                return None
            return body[0] | (body[1] << 8)
        return None

    def read(self):
        if self._ser:
            try:
                self._ser.reset_input_buffer()
                dist = self._read_frame()
                if dist is not None:
                    return {"front_cm": dist, "left_cm": None, "right_cm": None}
            except Exception as e:
                log.warning(f"LIDAR read error: {e}")
            return {"front_cm": last_front_cm, "left_cm": None, "right_cm": None}
        import random
        return {"front_cm": random.randint(20, 300), "left_cm": None, "right_cm": None}

    def close(self):
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass


def telemetry_loop(client):
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
            ("robot/cmd/test",   1),
            (VOICE_TOPIC,        0),
        ])
    else:
        log.error(f"Connect failed: {reason_code}")

def on_message(client, userdata, msg):
    global speed_pct

    if msg.topic == VOICE_TOPIC:
        handle_voice(msg.payload)
        return

    log.info(f"RX {msg.topic}  {msg.payload.decode()}")

    try:
        data = json.loads(msg.payload) if msg.payload else {}
    except Exception:
        data = {}

    if msg.topic == "robot/cmd/move":
        x = float(data.get("x", 0))
        y = float(data.get("y", 0))
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

    elif msg.topic == "robot/cmd/test":
        threading.Thread(target=motor_self_test, daemon=True).start()

def on_disconnect(client, userdata, reason_code, properties=None):
    log.warning(f"Disconnected: {reason_code}")
    stop_motors()

# ── Main ──────────────────────────────────────────────────
def main():
    global lidar
    if ON_PI:
        setup_gpio()

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

    threading.Thread(target=telemetry_loop, args=(client,), daemon=True).start()
    threading.Thread(target=voice_worker, daemon=True).start()

    def shutdown(sig, frame):
        global _telemetry_run, _voice_run
        _telemetry_run = False
        _voice_run = False
        stop_motors()
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