#!/usr/bin/env bash
set -e
cd -- "$(dirname -- "$0")/../.."
openbox --sm-disable >design/2026-11-calendar-disclosure/native-wm.log 2>&1 &
debacal_disclosure_wm=$!
trap 'kill "$debacal_disclosure_wm" 2>/dev/null || true' EXIT
QT_QPA_PLATFORM=xcb .venv/bin/python design/2026-11-calendar-disclosure/native_check.py
