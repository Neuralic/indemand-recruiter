#!/bin/bash
# restart the interview server in STUB voice mode (no Cartesia credits); usage: restart_stub.sh [REAL]
cd "$(dirname "$0")"
powershell.exe -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { \$_.CommandLine -match '26_live_interview' } | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force -ErrorAction SilentlyContinue }" >/dev/null 2>&1
sleep 2
if [ "$1" = "REAL" ]; then export REAL_VOICE=1; else unset REAL_VOICE; fi
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 nohup env -u PIPECAT_ICE_SERVERS .venv312/Scripts/python.exe 26_live_interview.py -t webrtc > out/_26_server.stdout 2>&1 &
disown
