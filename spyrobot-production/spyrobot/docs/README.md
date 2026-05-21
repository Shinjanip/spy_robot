# SpyRobot — Production Architecture Guide

## System overview

```
Raspberry Pi 4
  ├─ libcamera-vid → FFmpeg → RTSP push ──────────→ MediaMTX (laptop/VPS)
  │  (camera stream runs 24/7, independent of browsers)          │
  │                                                               │ WebRTC (WHEP)
  └─ Python controller ←→ MQTT ←→ HiveMQ Cloud ←→ Dashboard ────┘
     (motor/telemetry)  (TLS WSS)  (managed broker)  (browser)
```

**Key design principle**: The Raspberry Pi camera pipeline runs as a completely
independent process. It pushes RTSP to MediaMTX and keeps running regardless of
whether any browser is watching. A page refresh, tab close, or network hiccup
does not affect the camera stream at all. The browser simply reconnects to
MediaMTX (which has been continuously receiving video from the robot).

---

## Directory structure

```
spyrobot/
├── robot/                         # Runs on Raspberry Pi
│   ├── src/
│   │   ├── robot_controller.py    # Motor control + MQTT
│   │   └── stream.sh              # Camera streaming pipeline
│   ├── config/
│   │   ├── .env.example           # Environment variables template
│   │   └── spyrobot-*.service     # systemd service definitions
│   └── requirements.txt
│
├── server/                        # Runs on laptop (dev) or VPS (prod)
│   ├── docker-compose.yml         # MediaMTX + NGINX + Coturn
│   ├── mediamtx/
│   │   └── mediamtx.yml           # MediaMTX configuration
│   ├── nginx/
│   │   └── nginx.conf             # Reverse proxy + HTTPS
│   └── coturn/
│       └── turnserver.conf        # TURN server for WebRTC NAT
│
└── dashboard/
    └── index.html                 # Browser dashboard (single file)
```

---

## Phase 1: Laptop development setup

### 1. Start the server stack on your laptop

```bash
cd server
docker compose up -d
```

Services started:
- MediaMTX on ports 8554 (RTSP), 8889 (WebRTC), 8888 (HLS), 9997 (API)
- NGINX on port 80

Find your laptop's LAN IP:
```bash
# macOS
ipconfig getifaddr en0

# Linux
ip addr show | grep 'inet '
```

### 2. Configure the Raspberry Pi

```bash
# SSH into Pi
ssh pi@RASPBERRY_PI_IP

# Clone/copy the robot/ directory
git clone YOUR_REPO /home/pi/spyrobot
cd /home/pi/spyrobot/robot

# Create virtual environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure environment
sudo mkdir -p /etc/spyrobot
sudo cp config/.env.example /etc/spyrobot/.env
sudo nano /etc/spyrobot/.env
# → Set MEDIAMTX_HOST to your LAPTOP'S LAN IP
# → Set MQTT_HOST, MQTT_USER, MQTT_PASS for HiveMQ Cloud

# Test streaming (Ctrl+C to stop)
source /etc/spyrobot/.env
bash src/stream.sh

# In another terminal, test the controller
source venv/bin/activate
python src/robot_controller.py
```

### 3. Verify the stream

```bash
# On your laptop, play the stream
ffplay -fflags nobuffer rtsp://localhost:8554/spyrobot/live

# Or open in browser (WHEP WebRTC)
# → http://localhost:8889/spyrobot/live   (MediaMTX built-in player)
```

### 4. Open the dashboard

Open `dashboard/index.html` in your browser.
Update `CONFIG.whepUrl` to `http://LAPTOP_IP:8889/spyrobot/live/whep`

---

## MQTT topic structure

| Topic | Direction | QoS | Description |
|-------|-----------|-----|-------------|
| `robot/cmd/move` | Dashboard → Robot | 1 | Joystick XY: `{"x": 0.5, "y": 0.8}` |
| `robot/cmd/speed` | Dashboard → Robot | 1 | Speed percent: `{"speed": 60}` |
| `robot/cmd/stop` | Dashboard → Robot | 1 | Emergency stop |
| `robot/cmd/camera` | Dashboard → Robot | 1 | Pan/tilt: `{"pan": 0, "tilt": 15}` |
| `robot/telemetry` | Robot → Dashboard | 0 | Full telemetry bundle |
| `robot/telemetry/lidar` | Robot → Dashboard | 0 | LiDAR readings |
| `robot/telemetry/battery` | Robot → Dashboard | 0 | Battery percent |
| `robot/status` | Robot → All | 1 (retain) | Online/offline presence |

---

## Phase 2: Production VPS migration

### What changes

Only **one value** needs changing on the Raspberry Pi:
```bash
# In /etc/spyrobot/.env
MEDIAMTX_HOST=YOUR_VPS_PUBLIC_IP   # was laptop LAN IP
```

The robot controller (MQTT) doesn't change at all — HiveMQ Cloud is already
accessible from anywhere.

### VPS setup steps

```bash
# 1. Provision a VPS (DigitalOcean, Linode, Hetzner, etc.)
#    Minimum: 2 vCPU, 2 GB RAM, Ubuntu 22.04
#    Recommended for TURN: 4 GB RAM

# 2. Install Docker
curl -fsSL https://get.docker.com | sh

# 3. Copy server/ directory to VPS
rsync -av server/ user@VPS_IP:/opt/spyrobot/

# 4. Set up TLS with Let's Encrypt
apt install certbot
certbot certonly --standalone -d yourdomain.com
# Certs land in /etc/letsencrypt/live/yourdomain.com/

# 5. Update configuration
nano /opt/spyrobot/mediamtx/mediamtx.yml
# → Add your VPS IP to webrtcAdditionalHosts
# → Add TURN credentials to webrtcICEServers2

nano /opt/spyrobot/coturn/turnserver.conf
# → Set external-ip to your VPS public IP

nano /opt/spyrobot/nginx/nginx.conf
# → Uncomment the HTTPS server block
# → Comment out the HTTP-only dev block
# → Update server_name to your domain

# 6. Start everything
cd /opt/spyrobot
docker compose up -d

# 7. Update firewall
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 8554/tcp       # RTSP from Raspberry Pi
ufw allow 3478/tcp       # TURN
ufw allow 3478/udp       # TURN
ufw allow 49152:65535/udp  # TURN media relay range
```

### Update dashboard config for prod

```javascript
// dashboard/index.html → CONFIG object
const CONFIG = {
  whepUrl: 'https://yourdomain.com/stream/spyrobot/live/whep',
  mqttUrl: 'wss://YOUR_CLUSTER.hivemq.cloud:8884/mqtt',
  // ... rest unchanged
};
```

---

## Raspberry Pi install as system services (auto-start on boot)

```bash
# Install service files
sudo cp config/spyrobot-controller.service /etc/systemd/system/
sudo cp config/spyrobot-stream.service     /etc/systemd/system/

sudo systemctl daemon-reload

# Enable auto-start
sudo systemctl enable spyrobot-stream
sudo systemctl enable spyrobot-controller

# Start now
sudo systemctl start spyrobot-stream
sudo systemctl start spyrobot-controller

# Check status
sudo systemctl status spyrobot-stream
sudo journalctl -u spyrobot-controller -f
```

---

## Streaming optimization for teleoperation

### Latency budget (typical)
- Encode on Pi (libcamera H.264 HW): ~15–30ms
- RTSP over LAN to MediaMTX: ~5ms / over internet: ~20–80ms
- MediaMTX WebRTC WHEP delivery: ~30–80ms
- Browser decode: ~10–30ms
- **Total end-to-end: ~60–220ms typical**

### Tuning checklist
- [ ] Use `--profile baseline` — fastest decode start
- [ ] Use `--flush` on libcamera-vid — immediate frame delivery
- [ ] Keep keyframe interval ≤ 60 frames (2s at 30fps)
- [ ] Lower resolution for faster encode (720p > 1080p for latency)
- [ ] Use TCP for RTSP (more reliable than UDP)
- [ ] Set `llhls: yes` in MediaMTX for HLS fallback
- [ ] Use Coturn TURN server on same region as MediaMTX VPS

---

## Browser refresh resilience

**How it works:**
1. RPi streams RTSP → MediaMTX continuously (no browser involvement)
2. Browser loads dashboard → creates WHEP WebRTC connection to MediaMTX
3. Browser refreshes → WebRTC connection closes (browser side only)
4. RPi never knows about the browser closing
5. New page load → new WHEP connection → MediaMTX delivers the ongoing stream
6. Viewer sees the live stream within ~1–2 seconds

**The VideoPlayer in dashboard/index.html also:**
- Retries with exponential backoff on connection failure
- Reconnects on tab visibility change (coming back to the tab)
- Reconnects on ICE failure
- Has a manual retry button

---

## Multi-client viewing

MediaMTX supports unlimited simultaneous viewers by default.
Each viewer creates their own WHEP connection to MediaMTX.
The RPi always has exactly one RTSP connection to MediaMTX.

```
RPi → [1 RTSP] → MediaMTX → [N WHEP] → Browser 1
                                       → Browser 2
                                       → Mobile browser
                                       → Future native app
```

No configuration change needed for multi-viewer — it works out of the box.

---

## Security hardening checklist

- [ ] HiveMQ: use separate credentials for robot vs dashboard
- [ ] HiveMQ: configure ACLs so dashboard cannot publish to `robot/telemetry`
- [ ] MediaMTX: enable auth (authMethod: internal) before going public
- [ ] NGINX: restrict RTSP port (8554) to RPi IP only via firewall
- [ ] VPS firewall: block all ports except 80, 443, 8554, 3478, 49152-65535
- [ ] TLS: force HTTPS in production
- [ ] Secrets: never commit .env files — use environment variables in CI/CD
- [ ] TURN: use time-limited credentials (HMAC) for production
