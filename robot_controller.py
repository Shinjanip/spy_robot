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
    if ON_PI:
        setup_gpio()

    # Start audio watchdog thread
#    threading.Thread(target=_audio_watchdog, daemon=True).start()

    # Auto-start audio on boot
 #   start_audio()

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

    def shutdown(sig, frame):
        stop_motors()
        stop_audio()
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
