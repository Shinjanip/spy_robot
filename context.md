# SpyRobot — Project Context & Architecture

A teleoperated "spy robot" on a Raspberry Pi 4, controllable from anywhere via a
web dashboard. Live video + audio, joystick driving, two-way audio, and LiDAR
obstacle detection. Hosted through a Cloudflare tunnel — **no VPS / physical
server**.

---

## 1. Hardware

| Component            | Detail                                                        |
|---------------------|---------------------------------------------------------------|
| Compute             | Raspberry Pi 4 Model B                                         |
| Camera              | Pi Camera module (via `rpicam-vid`)                           |
| Microphone          | USB mic (ALSA `plughw:1,0`)                                    |
| Speaker             | Speaker via **MAX98357A** I2S amplifier (ALSA `plughw:2,0`)    |
| LiDAR               | Benewake **TF-Luna / TFmini** single-beam, **UART** `/dev/serial0` |
| Motors              | DC motors via **L298N** H-bridge                              |
| Power               | (battery — telemetry not yet wired)                            |

---

## 2. High-level architecture

```
        Browser (anywhere)
        ┌────────────────────────────────────────┐
        │  index.html dashboard                   │
        │   • WebRTC video/audio  (go2rtc)        │
        │   • Push-to-talk mic    (go2rtc)        │
        │   • Joystick / keys     (MQTT)          │
        │   • LiDAR readout       (MQTT)          │
        └───────┬───────────────────────┬─────────┘
                │ wss /api/ws            │ wss (MQTT)
                │ (signalling)           │
        ┌───────▼─────────┐      ┌───────▼─────────────┐
        │ Cloudflare      │      │  HiveMQ Cloud       │
        │ Tunnel → :1984  │      │  (MQTT broker, TLS) │
        └───────┬─────────┘      └───────┬─────────────┘
                │                        │
   ┌────────────▼──────────┐   ┌─────────▼───────────────┐
   │ go2rtc (Pi, :1984)    │   │ robot_controller.py (Pi)│
   │  • spy_cam (cam+mic)  │   │  • motors (L298N)       │
   │  • speaker_in (spkr)  │   │  • TF-Luna LiDAR read   │
   │  • WebRTC :8555/tcp   │   │  • obstacle auto-stop   │
   │  • serves dashboard   │   │  • publishes telemetry  │
   └───────────────────────┘   └─────────────────────────┘
```

WebRTC **media** (video/audio packets) does **not** flow through the Cloudflare
tunnel — only the signalling WebSocket does. Media needs a direct path or a TURN
relay (see §7 Open Decisions).

---

## 3. Components

### go2rtc (`go2rtc.yaml`)
Streaming server on the Pi. Serves the dashboard (`static_dir`) and the
`/api/ws` signalling WebSocket on **:1984**; WebRTC media on **:8555/tcp**.

- `spy_cam` stream = `rpicam-vid` (H.264 video) **+** `ffmpeg` ALSA USB-mic
  (Opus audio). Browser receives both from this one stream.
- `speaker_in` stream = `alsa:plughw:2,0` — receives browser mic audio and plays
  it out the MAX98357A → speaker.
- `webrtc.ice_servers` = Cloudflare TURN (credentials are placeholders — see §7).

### MQTT (HiveMQ Cloud)
TLS broker for control + telemetry. Robot uses port 8883 (native MQTT/TLS);
browser uses 8884 (MQTT-over-WSS).

| Topic                    | Dir        | Payload                              |
|--------------------------|------------|--------------------------------------|
| `robot/cmd/move`         | → robot    | `{ "x": -1..1, "y": -1..1 }`         |
| `robot/cmd/stop`         | → robot    | `{ "reason": "..." }`                |
| `robot/cmd/speed`        | → robot    | `{ "speed": 10..100 }`               |
| `robot/cmd/camera`       | → robot    | `{ "pan":.., "tilt":.. }` (stub)     |
| `robot/cmd/audio`        | → robot    | `{ "enabled": bool }` (no-op stub)   |
| `robot/telemetry/lidar`  | → browser  | `{ front_cm, left_cm, right_cm, obstacle, warn_cm, stop_cm }` |

### robot_controller.py
Module-level (not class-based) MQTT client driving the L298N and reading LiDAR.

- **Motors:** `set_motors()` / `xy_to_motors()` differential drive. Pins (BCM):
  `L_FWD/L_BWD/L_PWM = 16,16,20`, `R_FWD/R_BWD/R_PWM = 13,13,26`.
  ⚠️ FWD and BWD share a pin per side (no reverse with current wiring — verify
  against your actual hardware).
- **LiDAR:** `LidarReader` parses the TF-Luna 9-byte UART frame
  (`0x59 0x59 distL distH …` + checksum), returns `front_cm`.
- **Telemetry:** `telemetry_loop()` thread publishes `robot/telemetry/lidar`
  at `TELEMETRY_HZ` (5 Hz).
- **Obstacle safety:** forward motion (`y > 0`) is blocked when
  `0 ≤ front_cm < OBSTACLE_STOP_CM` (25 cm). `OBSTACLE_WARN_CM` = 60 cm.
- **Audio:** handled entirely by go2rtc; `start_audio()`/`stop_audio()` are
  safe no-op stubs.

### Dashboard (`index.html`)
Single-file UI served by go2rtc. Connects WS to `/api/ws` (same origin as page).

- **Video/audio in:** WebRTC from `spy_cam` via `connectWS`. ICE servers from
  `getIceServers()` — fetches `TURN_CREDS_URL` if set, else inline `ICE_SERVERS`.
- **Audio (half-duplex):** **Listen** toggle (mute incoming) + **Hold-to-talk**
  (button or `T` key). While talking, incoming audio auto-mutes to kill the
  echo/feedback loop. Operator mic uses echo cancellation / noise suppression /
  auto gain.
- **Driving:** joystick (pointer) + WASD/arrows, `Space` = stop. Publishes
  `robot/cmd/move` at 10 Hz.
- **LiDAR UI:** `renderLidar()` shows live distance + clear / ⚠ close / ⛔ BLOCKED.

### Cloudflare tunnel
Exposes the site + `/api/ws` to the internet. Because go2rtc serves both on
:1984, a single ingress rule covers everything:

```yaml
# ~/.cloudflared/config.yml (named tunnel)
ingress:
  - hostname: spyrobot.yourdomain.com
    service: http://localhost:1984     # page + /api/ws (WS upgrade automatic)
  - service: http_status:404
```
Currently a **temporary quick tunnel** (`cloudflared tunnel --url http://localhost:1984`)
is in use → random `*.trycloudflare.com` URL that changes per restart.

### turn-worker/ (optional)
Cloudflare Worker that mints short-lived TURN credentials so they never expire
and the secret stays server-side. Point `TURN_CREDS_URL` in `index.html` at it.
See `turn-worker/README.md`.

---

## 4. Recent additions (this work)

1. **LiDAR** — real TF-Luna UART driver, telemetry publishing, obstacle
   auto-stop, and a live distance/obstacle readout on the dashboard.
2. **Half-duplex audio** — push-to-talk + listen-mute + echo cancellation to
   stop the speaker→mic feedback howl and give the operator real control.
3. **Cloudflare TURN** — `ice_servers` on both go2rtc and the browser so remote
   viewers behind CGNAT can connect (replaces the stale hardcoded public IP and
   fixes the "connect-then-drop / page reload" loop).
4. **turn-worker** — auto-refreshing TURN credential minter.

Commits: `869814a` (lidar + audio + TURN), `00dfadd` (turn-worker).

---

## 5. Problems these solved

| Symptom (before)                                   | Cause                                        | Fix |
|----------------------------------------------------|----------------------------------------------|-----|
| Remote viewers: page reloads, stream connects then drops | Media couldn't reach the Pi's public IP:8555 (CGNAT / no port-forward); YAML had a broken `candidates` indent + stale IP | TURN relay on both peers |
| Mic + speaker always on, feedback howl, no control | Full-duplex audio; robot mic picks up robot speaker | Half-duplex push-to-talk + listen-mute + AEC |
| LiDAR distance not shown                            | `LidarReader` was a stub returning zeros     | Real TF-Luna driver + telemetry + UI |

---

## 6. Setup / deploy checklist

**On the Pi:**
- `pip install pyserial paho-mqtt`
- Enable UART: `sudo raspi-config` → Interface → Serial (login shell **No**,
  hardware **Yes**) → reboot; confirm `ls -l /dev/serial0`.
- Wire TF-Luna: 5V, GND, and **cross** TX↔RX (Luna TXD→GPIO15, Luna RXD→GPIO14).
- Place `index.html` in go2rtc's `static_dir` (`/home/raspberry4/go2rtc/www`).
- Run go2rtc, `robot_controller.py`, and the cloudflared tunnel.

**Credentials to fill in:**
- TURN `username`/`credential` → `go2rtc.yaml` + `index.html`
  (`PASTE_CLOUDFLARE_TURN_*`), or deploy `turn-worker` and set `TURN_CREDS_URL`.
- MQTT creds are already in both files (HiveMQ cluster).

---

## 7. Open decisions & known issues

### ⚠ DECISION PENDING — remote streaming relay
Cloudflare TURN's free tier (1,000 GB) currently **requires adding a payment
card** despite "no credit card" marketing. Three options on the table:

1. **MSE over the tunnel (recommended)** — stream video+audio as fMP4 over the
   existing WebSocket; **no TURN, no card, no port-forward, immune to CGNAT.**
   Talk-back becomes push-to-talk. Requires reworking the `index.html` video path.
2. **Free Metered "Open Relay" TURN** — keep WebRTC + two-way audio; free signup,
   no card, ~20 GB/mo, community-grade reliability.
3. **Add a card to Cloudflare** — lowest-latency full two-way WebRTC; realistically
   never billed under 1,000 GB.

### Known issues / TODO
- **WebSocket idle timeout:** Cloudflare drops idle WebSockets (~100 s). The
  signalling WS goes idle once WebRTC connects, and `index.html` currently closes
  the peer connection on WS close → a needless video reconnect every ~100 s.
  Fix: send a keepalive over the WS and/or don't tear down an already-connected
  `pc`. (Not yet applied.)
- **Motor wiring:** FWD/BWD share a GPIO per side — reverse may not work; verify.
- **Battery telemetry:** not implemented.
- **LiDAR is single-beam** (front only); left/right are `null`. Mount on a servo
  for wider coverage.
- **Quick tunnel is temporary** — move to a named tunnel for a stable URL.

---

## 8. File map

| File                     | Purpose                                            |
|--------------------------|----------------------------------------------------|
| `robot_controller.py`    | Motors, LiDAR, obstacle stop, telemetry (Pi)       |
| `go2rtc.yaml`            | Streaming server config (cam/mic/speaker, WebRTC)  |
| `index.html`             | Web dashboard (**the deployed one**, served by go2rtc) |
| `turn-worker/`           | Cloudflare Worker: TURN credential minter          |
| `audio_stream.sh`, `stream.sh`, `i2samp.py` | Audio/stream helper scripts (Pi) |
| `context.md`             | This document                                       |

> Note: `dashboard.html` / `spyrobot-production/` exist on other branches and are
> not the deployed dashboard. The repo lives under OneDrive — verify file
> contents against git before large edits (OneDrive can serve stale copies).
