#!/usr/bin/env bash
# SpyRobot stream — Raspberry Pi (Bookworm, Camera Module v2)
# Uses rpicam-vid with built-in TCP socket output → FFmpeg mux → MediaMTX RTSP

MEDIAMTX_HOST="${MEDIAMTX_HOST:-192.168.1.6}"
MEDIAMTX_PORT="${MEDIAMTX_PORT:-8554}"
RTSP_URL="rtsp://${MEDIAMTX_HOST}:${MEDIAMTX_PORT}/spyrobot/live"

echo "Target: $RTSP_URL"

while true; do
    echo "[$(date)] Starting pipeline..."

    # Step 1: rpicam-vid writes H.264 to a local TCP socket
    # Step 2: FFmpeg reads from that socket and pushes RTSP to MediaMTX
    # This avoids the stdout pipe race condition entirely.

    rpicam-vid \
        --width 1280 --height 720 \
        --framerate 30 \
        --bitrate 2000000 \
        --codec h264 \
        --profile baseline \
        --level 4.1 \
        --intra 30 \
        --inline \
        --listen \
        --timeout 0 \
        --nopreview \
        -o tcp://127.0.0.1:8765 &

    RPICAM_PID=$!

    # Give camera 3 seconds to open the TCP socket and configure
    sleep 3

    # Check camera actually started
    if ! kill -0 $RPICAM_PID 2>/dev/null; then
        echo "[$(date)] rpicam-vid failed to start. Check: rpicam-hello"
        sleep 5
        continue
    fi

    ffmpeg \
        -hide_banner \
        -loglevel warning \
        -i tcp://127.0.0.1:8765 \
        -c:v copy \
        -f rtsp \
        -rtsp_transport tcp \
        "$RTSP_URL"

    echo "[$(date)] FFmpeg exited. Killing camera..."
    kill $RPICAM_PID 2>/dev/null
    wait $RPICAM_PID 2>/dev/null
    sleep 3
done
