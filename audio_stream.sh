#!/bin/bash
# Two-way audio using FFmpeg + RTSP (works with MediaMTX)

MEDIAMTX_HOST="${MEDIAMTX_HOST:-192.168.1.12}"
RTSP_BASE="rtsp://${MEDIAMTX_HOST}:8554"

# USB microphone – adjust after `arecord -l`
USB_MIC="hw:1,0"

# Speaker output – uses ALSA softvol as default
SPEAKER="default"

# === OUT: Pi microphone → MediaMTX → Browser ===
# Push USB mic as RTSP stream
ffmpeg -hide_banner -loglevel warning \
    -f alsa -sample_rate 48000 -channels 1 -i "$USB_MIC" \
    -c:a libopus -b:a 64k \
    -f rtsp -rtsp_transport tcp \
    "${RTSP_BASE}/spyrobot/audio/out" \
    2>&1 | sed 's/^/[OUT] /' &

# === IN: Browser → MediaMTX → Pi speaker ===
# Pull browser audio as RTSP stream and play on speaker
ffmpeg -hide_banner -loglevel warning \
    -rtsp_transport tcp -i "${RTSP_BASE}/spyrobot/audio/in" \
    -c:a pcm_s16le -f alsa "$SPEAKER" \
    2>&1 | sed 's/^/[IN] /' &

wait
