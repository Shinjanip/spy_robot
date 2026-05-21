#!/usr/bin/env bash
# ============================================================
# SpyRobot — Camera Streaming Launcher
# Raspberry Pi 4 Model B + Camera Module v2/v3
#
# Strategy:
#   libcamera-vid  →  H.264 hardware encoder (VideoCore IV)
#   stdout pipe    →  FFmpeg  →  RTSP push  →  MediaMTX
#
# The camera pipeline runs INDEPENDENTLY of the browser.
# MediaMTX handles all WebRTC delivery to viewers.
# A browser refresh has zero impact on this pipeline.
# ============================================================

set -euo pipefail

# ── Configuration (override with env vars) ──────────────────
MEDIAMTX_HOST="${MEDIAMTX_HOST:-192.168.1.100}"   # laptop IP during dev
MEDIAMTX_PORT="${MEDIAMTX_PORT:-8554}"
STREAM_PATH="${STREAM_PATH:-spyrobot/live}"
STREAM_WIDTH="${STREAM_WIDTH:-1280}"
STREAM_HEIGHT="${STREAM_HEIGHT:-720}"
STREAM_FPS="${STREAM_FPS:-30}"
STREAM_BITRATE="${STREAM_BITRATE:-2000000}"        # 2 Mbps — good for teleoperation
STREAM_KEYFRAME_INTERVAL="${STREAM_KEYFRAME_INTERVAL:-60}"  # 2 sec @ 30fps

RTSP_URL="rtsp://${MEDIAMTX_HOST}:${MEDIAMTX_PORT}/${STREAM_PATH}"

echo "==================================================="
echo "  SpyRobot Camera Stream"
echo "  Resolution : ${STREAM_WIDTH}x${STREAM_HEIGHT} @ ${STREAM_FPS}fps"
echo "  Bitrate    : ${STREAM_BITRATE} bps"
echo "  Target     : ${RTSP_URL}"
echo "==================================================="

# ── Wait for camera to be ready ─────────────────────────────
wait_for_camera() {
    echo "Waiting for camera…"
    for i in $(seq 1 10); do
        if libcamera-hello --timeout 1 --nopreview 2>/dev/null; then
            echo "Camera ready."
            return 0
        fi
        echo "  attempt $i/10…"
        sleep 2
    done
    echo "ERROR: Camera not detected after 10 attempts."
    exit 1
}

# ── Reconnect loop ───────────────────────────────────────────
# If MediaMTX or the network drops, this loop restarts FFmpeg.
# The Raspberry Pi never stops trying to stream.
stream_loop() {
    local retry_delay=3

    while true; do
        echo "$(date) — Starting camera pipeline…"

        # ── PRODUCTION PIPELINE ─────────────────────────────
        # libcamera-vid: hardware H.264 encoder
        #   --codec h264        → use VideoCore H.264 encoder
        #   --inline            → embed SPS/PPS in every IDR frame (critical for reconnect)
        #   --level 4.2         → widely compatible
        #   --profile baseline  → lowest latency decode
        #   -t 0                → stream indefinitely
        #   -o -               → pipe to stdout
        #
        # FFmpeg: mux into RTSP and push to MediaMTX
        #   -re                 → read at real-time rate (prevents burst)
        #   -f h264             → input is raw H.264 Annex B
        #   -vcodec copy        → NO TRANSCODING — use the HW-encoded stream as-is
        #   -f rtsp             → RTSP output
        #   -rtsp_transport tcp → TCP more reliable than UDP over internet

        libcamera-vid \
            --codec h264 \
            --width  "${STREAM_WIDTH}" \
            --height "${STREAM_HEIGHT}" \
            --framerate "${STREAM_FPS}" \
            --bitrate "${STREAM_BITRATE}" \
            --intra "${STREAM_KEYFRAME_INTERVAL}" \
            --inline \
            --level 4.2 \
            --profile baseline \
            --nopreview \
            --flush \
            --timeout 0 \
            --output - \
        | ffmpeg \
            -hide_banner \
            -loglevel warning \
            -re \
            -f h264 \
            -r "${STREAM_FPS}" \
            -i pipe:0 \
            -c:v copy \
            -f rtsp \
            -rtsp_transport tcp \
            "${RTSP_URL}"

        EXIT_CODE=$?
        echo "$(date) — Pipeline exited (code ${EXIT_CODE}). Retry in ${retry_delay}s…"
        sleep "${retry_delay}"
    done
}

# ── Alternative: Direct libcamera → RTSP (newer MediaMTX) ───
# If you're using MediaMTX >= 1.4 with its built-in RTSP server source,
# you can skip FFmpeg entirely and configure MediaMTX to run libcamera-vid
# as a runOnDemand command. See mediamtx.yml for that config.

# ── Low-latency tuning notes ─────────────────────────────────
# For sub-200ms latency teleoperation:
#   1. Keep keyframe interval <= 60 frames (2 sec at 30fps)
#   2. Use baseline profile (decoder starts faster)
#   3. Use --flush flag (flushes encoder immediately, no buffering)
#   4. In MediaMTX set llhls: yes for HLS fallback
#   5. WebRTC via WHEP from MediaMTX adds ~50-150ms on top of encode

wait_for_camera
stream_loop


# ============================================================
# ALTERNATIVE COMMANDS FOR DIFFERENT SCENARIOS
# ============================================================

# A) Lower latency (720p 30fps, 1.5 Mbps) — best for teleoperation
# libcamera-vid --codec h264 -w 1280 -h 720 --framerate 30 \
#   --bitrate 1500000 --inline --profile baseline --nopreview -t 0 -o - \
#   | ffmpeg -f h264 -i pipe:0 -c:v copy -f rtsp \
#     -rtsp_transport tcp rtsp://SERVER:8554/spyrobot/live

# B) High quality (1080p 25fps, 4 Mbps) — for inspection/recording
# libcamera-vid --codec h264 -w 1920 -h 1080 --framerate 25 \
#   --bitrate 4000000 --inline --profile main --nopreview -t 0 -o - \
#   | ffmpeg -f h264 -i pipe:0 -c:v copy -f rtsp \
#     -rtsp_transport tcp rtsp://SERVER:8554/spyrobot/hq

# C) Test stream (generate a pattern without a camera)
# ffmpeg -re -f lavfi -i testsrc=size=1280x720:rate=30 \
#   -c:v libx264 -preset ultrafast -tune zerolatency -b:v 2M \
#   -f rtsp -rtsp_transport tcp rtsp://SERVER:8554/spyrobot/test

# D) View the stream locally for debugging
# ffplay -fflags nobuffer -flags low_delay -framedrop \
#   rtsp://SERVER:8554/spyrobot/live
