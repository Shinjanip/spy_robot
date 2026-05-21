"""
SpyRobot - Main Robot Controller
Raspberry Pi 4 Model B

Responsibilities:
  - Subscribe to MQTT command topics
  - Control DC motors via motor driver
  - Read LiDAR sensor data
  - Publish telemetry (battery, speed, lidar, status)
  - Heartbeat / watchdog (stop motors on command timeout)

Video streaming is handled ENTIRELY by FFmpeg/libcamera
running as a separate process. This module never touches
the camera or any WebRTC peer connection.
"""

import time
import json
import threading
import logging
import signal
import sys
import os
from dataclasses import dataclass, asdict
from typing import Optional

import paho.mqtt.client as mqtt

# ─── optional hardware imports (mock if not on RPi) ────────────────────────
try:
    import RPi.GPIO as GPIO
    ON_PI = True
except ImportError:
    ON_PI = False
    logging.warning("RPi.GPIO not found – running in simulation mode")

try:
    import smbus2
    HAS_SMBUS = True
except ImportError:
    HAS_SMBUS = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/var/log/spyrobot.log"),
    ],
)
log = logging.getLogger("spyrobot")


# ─── Configuration ─────────────────────────────────────────────────────────
@dataclass
class Config:
    # HiveMQ Cloud MQTT broker
    mqtt_host: str = os.getenv("MQTT_HOST", "YOUR_CLUSTER.hivemq.cloud")
    mqtt_port: int = int(os.getenv("MQTT_PORT", "8883"))          # TLS port
    mqtt_user: str = os.getenv("MQTT_USER", "robot_user")
    mqtt_pass: str = os.getenv("MQTT_PASS", "YOUR_PASSWORD")
    mqtt_client_id: str = "spyrobot-pi-001"
    mqtt_keepalive: int = 30

    # Topics
    topic_cmd_move: str = "robot/cmd/move"
    topic_cmd_speed: str = "robot/cmd/speed"
    topic_cmd_stop: str = "robot/cmd/stop"
    topic_cmd_camera: str = "robot/cmd/camera"
    topic_telemetry: str = "robot/telemetry"
    topic_status: str = "robot/status"
    topic_lidar: str = "robot/telemetry/lidar"
    topic_battery: str = "robot/telemetry/battery"

    # GPIO pin assignments (BCM mode)
    # L298N motor driver or similar
    motor_left_forward: int = 17
    motor_left_backward: int = 18
    motor_left_pwm: int = 27
    motor_right_forward: int = 22
    motor_right_backward: int = 23
    motor_right_pwm: int = 24

    # Safety
    watchdog_timeout_sec: float = 2.0   # stop motors if no cmd received
    telemetry_interval_sec: float = 0.5
    max_speed: int = 100                 # PWM duty cycle max
    pwm_freq: int = 1000                 # Hz

    # Battery ADC (optional MCP3008 on SPI)
    battery_max_v: float = 12.6
    battery_min_v: float = 9.0


cfg = Config()


# ─── Motor Controller ───────────────────────────────────────────────────────
class MotorController:
    """
    Dual H-bridge motor driver control.
    Positive speed = forward, negative = backward.
    Range: -100 to +100.
    """

    def __init__(self, config: Config):
        self.cfg = config
        self._left_speed = 0
        self._right_speed = 0
        self._lock = threading.Lock()
        self._pwm_left: Optional[object] = None
        self._pwm_right: Optional[object] = None

        if ON_PI:
            self._setup_gpio()

    def _setup_gpio(self):
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)

        pins = [
            self.cfg.motor_left_forward,
            self.cfg.motor_left_backward,
            self.cfg.motor_left_pwm,
            self.cfg.motor_right_forward,
            self.cfg.motor_right_backward,
            self.cfg.motor_right_pwm,
        ]
        GPIO.setup(pins, GPIO.OUT, initial=GPIO.LOW)

        self._pwm_left = GPIO.PWM(self.cfg.motor_left_pwm, self.cfg.pwm_freq)
        self._pwm_right = GPIO.PWM(self.cfg.motor_right_pwm, self.cfg.pwm_freq)
        self._pwm_left.start(0)
        self._pwm_right.start(0)
        log.info("GPIO and PWM initialized")

    def set_motors(self, left: float, right: float):
        """
        Set motor speeds.
        left, right: -100.0 to +100.0
        """
        left = max(-self.cfg.max_speed, min(self.cfg.max_speed, left))
        right = max(-self.cfg.max_speed, min(self.cfg.max_speed, right))

        with self._lock:
            self._left_speed = left
            self._right_speed = right

        if ON_PI:
            self._apply_motor(
                left,
                self.cfg.motor_left_forward,
                self.cfg.motor_left_backward,
                self._pwm_left,
            )
            self._apply_motor(
                right,
                self.cfg.motor_right_forward,
                self.cfg.motor_right_backward,
                self._pwm_right,
            )
        else:
            log.info(f"[SIM] Motors L={left:.1f} R={right:.1f}")

    def _apply_motor(self, speed, pin_fwd, pin_bwd, pwm):
        if speed > 0:
            GPIO.output(pin_fwd, GPIO.HIGH)
            GPIO.output(pin_bwd, GPIO.LOW)
        elif speed < 0:
            GPIO.output(pin_fwd, GPIO.LOW)
            GPIO.output(pin_bwd, GPIO.HIGH)
        else:
            GPIO.output(pin_fwd, GPIO.LOW)
            GPIO.output(pin_bwd, GPIO.LOW)
        pwm.ChangeDutyCycle(abs(speed))

    def stop(self):
        self.set_motors(0, 0)

    @property
    def speeds(self):
        with self._lock:
            return self._left_speed, self._right_speed

    def cleanup(self):
        self.stop()
        if ON_PI:
            if self._pwm_left:
                self._pwm_left.stop()
            if self._pwm_right:
                self._pwm_right.stop()
            GPIO.cleanup()


# ─── Joystick → Motor translator ───────────────────────────────────────────
def joystick_to_motors(x: float, y: float, speed_pct: float = 100.0):
    """
    Convert joystick XY (−1..+1 each) to differential drive speeds.
    Returns (left_speed, right_speed) in range −100..+100.
    """
    scale = speed_pct / 100.0
    left  = (y + x) * 100.0 * scale
    right = (y - x) * 100.0 * scale
    # Normalize so neither exceeds ±100
    max_val = max(abs(left), abs(right), 1.0)
    if max_val > 100.0:
        left  /= max_val / 100.0
        right /= max_val / 100.0
    return left, right


# ─── LiDAR Reader (stub — replace with your sensor SDK) ────────────────────
class LidarReader:
    """
    Replace this stub with your actual LiDAR SDK.
    Common choices: rplidar (RPLidar A1/A2), TFmini (UART), VL53L1X (I2C).
    """

    def read(self) -> dict:
        if ON_PI:
            # TODO: replace with actual sensor read
            return {"front_cm": 0, "left_cm": 0, "right_cm": 0}
        else:
            import random
            return {
                "front_cm": random.randint(10, 300),
                "left_cm":  random.randint(10, 300),
                "right_cm": random.randint(10, 300),
            }


# ─── Battery Reader (stub) ──────────────────────────────────────────────────
class BatteryReader:
    """
    Read battery voltage via ADC.
    Example: MCP3008 on SPI, or INA219 on I2C.
    """

    def read_percent(self) -> float:
        if not ON_PI:
            return 87.0
        # TODO: replace with actual ADC read
        return 75.0


# ─── Main Robot class ───────────────────────────────────────────────────────
class SpyRobot:
    def __init__(self):
        self.cfg = cfg
        self.motors = MotorController(cfg)
        self.lidar = LidarReader()
        self.battery = BatteryReader()
        self._speed_pct = 60.0
        self._watchdog_timer: Optional[threading.Timer] = None
        self._running = False

        # MQTT client with TLS
        self.mqtt = mqtt.Client(
            client_id=cfg.mqtt_client_id,
            protocol=mqtt.MQTTv5,
        )
        self.mqtt.username_pw_set(cfg.mqtt_user, cfg.mqtt_pass)
        self.mqtt.tls_set()   # uses system CA bundle; HiveMQ Cloud uses a valid cert

        # Last Will: publish offline status if we disconnect unexpectedly
        self.mqtt.will_set(
            cfg.topic_status,
            json.dumps({"online": False, "client_id": cfg.mqtt_client_id}),
            qos=1,
            retain=True,
        )

        self.mqtt.on_connect    = self._on_connect
        self.mqtt.on_disconnect = self._on_disconnect
        self.mqtt.on_message    = self._on_message

    # ── MQTT callbacks ──────────────────────────────────────────────────────
    def _on_connect(self, client, userdata, flags, rc, props=None):
        if rc == 0:
            log.info("MQTT connected to HiveMQ Cloud")
            # Subscribe to all command topics
            client.subscribe([
                (self.cfg.topic_cmd_move,   1),
                (self.cfg.topic_cmd_speed,  1),
                (self.cfg.topic_cmd_stop,   1),
                (self.cfg.topic_cmd_camera, 1),
            ])
            # Announce online
            self._publish_status(online=True)
        else:
            log.error(f"MQTT connect failed rc={rc}")

    def _on_disconnect(self, client, userdata, rc, props=None):
        log.warning(f"MQTT disconnected rc={rc} — motors stopping")
        self.motors.stop()

    def _on_message(self, client, userdata, msg: mqtt.MQTTMessage):
        try:
            payload = json.loads(msg.payload.decode())
            log.debug(f"MQTT rx {msg.topic}: {payload}")

            if msg.topic == self.cfg.topic_cmd_move:
                self._handle_move(payload)
            elif msg.topic == self.cfg.topic_cmd_speed:
                self._handle_speed(payload)
            elif msg.topic == self.cfg.topic_cmd_stop:
                self.motors.stop()
            elif msg.topic == self.cfg.topic_cmd_camera:
                self._handle_camera(payload)

        except (json.JSONDecodeError, KeyError) as e:
            log.warning(f"Bad payload on {msg.topic}: {e}")

    # ── Command handlers ────────────────────────────────────────────────────
    def _handle_move(self, payload: dict):
        """
        Expected payload:
          { "x": -1.0..1.0, "y": -1.0..1.0 }
          x = turn left/right, y = forward/backward
        """
        x = float(payload.get("x", 0.0))
        y = float(payload.get("y", 0.0))
        left, right = joystick_to_motors(x, y, self._speed_pct)
        self.motors.set_motors(left, right)
        self._reset_watchdog()

    def _handle_speed(self, payload: dict):
        """{ "speed": 0..100 }"""
        self._speed_pct = float(payload.get("speed", 60.0))
        log.info(f"Speed set to {self._speed_pct}%")

    def _handle_camera(self, payload: dict):
        """
        Camera pan/tilt if servo fitted.
        { "pan": -90..90, "tilt": -45..45 }
        Extend here with servo control.
        """
        pan  = payload.get("pan",  0)
        tilt = payload.get("tilt", 0)
        log.info(f"Camera pan={pan} tilt={tilt} (servo not yet wired)")

    # ── Watchdog ────────────────────────────────────────────────────────────
    def _reset_watchdog(self):
        if self._watchdog_timer:
            self._watchdog_timer.cancel()
        self._watchdog_timer = threading.Timer(
            self.cfg.watchdog_timeout_sec, self._watchdog_fire
        )
        self._watchdog_timer.daemon = True
        self._watchdog_timer.start()

    def _watchdog_fire(self):
        log.warning("Watchdog: no move command received — stopping motors")
        self.motors.stop()

    # ── Telemetry publisher ─────────────────────────────────────────────────
    def _telemetry_loop(self):
        while self._running:
            try:
                left_spd, right_spd = self.motors.speeds
                lidar = self.lidar.read()
                batt  = self.battery.read_percent()

                telemetry = {
                    "ts": time.time(),
                    "battery_pct": batt,
                    "motor_left":  left_spd,
                    "motor_right": right_spd,
                    "speed_pct":   self._speed_pct,
                    "lidar":       lidar,
                }
                self.mqtt.publish(
                    self.cfg.topic_telemetry,
                    json.dumps(telemetry),
                    qos=0,    # telemetry is fire-and-forget
                )
                # Publish lidar separately for subscribers that only want sensor data
                self.mqtt.publish(
                    self.cfg.topic_lidar,
                    json.dumps(lidar),
                    qos=0,
                )
                self.mqtt.publish(
                    self.cfg.topic_battery,
                    json.dumps({"pct": batt}),
                    qos=0,
                )
            except Exception as e:
                log.error(f"Telemetry error: {e}")

            time.sleep(self.cfg.telemetry_interval_sec)

    def _publish_status(self, online: bool):
        payload = {
            "online":    online,
            "client_id": self.cfg.mqtt_client_id,
            "ts":        time.time(),
        }
        self.mqtt.publish(self.cfg.topic_status, json.dumps(payload), qos=1, retain=True)

    # ── Lifecycle ───────────────────────────────────────────────────────────
    def start(self):
        log.info("SpyRobot starting…")
        self._running = True

        self.mqtt.connect_async(
            self.cfg.mqtt_host,
            self.cfg.mqtt_port,
            self.cfg.mqtt_keepalive,
        )
        self.mqtt.loop_start()

        telemetry_thread = threading.Thread(
            target=self._telemetry_loop, daemon=True, name="telemetry"
        )
        telemetry_thread.start()

        log.info("SpyRobot running — waiting for commands")

    def stop(self):
        log.info("SpyRobot shutting down…")
        self._running = False
        self.motors.stop()
        self._publish_status(online=False)
        self.mqtt.loop_stop()
        self.mqtt.disconnect()
        self.motors.cleanup()


# ─── Entry point ────────────────────────────────────────────────────────────
def main():
    robot = SpyRobot()
    robot.start()

    def _shutdown(sig, frame):
        robot.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Keep main thread alive
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
