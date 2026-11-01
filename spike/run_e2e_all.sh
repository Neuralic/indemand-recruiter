#!/bin/bash
# Phase 0b: every e2e session, strictly one at a time; up to 3 attempts each until 10 valid turns (no local stalls).
cd /c/Projects/indemand-recruiter/spike
export PYTHONIOENCODING=utf-8 FISH_MODEL_OVERRIDE=s2.1-pro-free
run() {  # run <json-name> <env assignments...> -- <args...>
  name=$1; shift
  for attempt in 1 2 3; do
    echo "$(date +%T) START $name attempt $attempt free_ram=$(powershell -NoProfile -Command "[math]::Round((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory/1024)")MB"
    env "$@" 2>&1 | grep -E "^13_e2e_|Traceback" | cut -c1-250
    n=$(python -c "import json;print(json.load(open('out/$name.json')).get('n_valid',-1))" 2>/dev/null)
    echo "$(date +%T) END $name attempt $attempt n_valid=$n"
    [ "$n" -ge 10 ] 2>/dev/null && break
    sleep 5
  done
}
run 13_e2e_A_marin_server E2E_TURN=server uv run python 13_e2e.py A marin
for v in Harlow Max Maren; do run 13_e2e_B_$(echo $v | tr A-Z a-z) uv run python 13_e2e.py B $v; done
for v in Laura Abby Ethan; do run 13_e2e_C_$(echo $v | tr A-Z a-z) uv run python 13_e2e.py C $v; done
for v in Max Maren; do run 13_e2e_D_$(echo $v | tr A-Z a-z)_cartesia_server E2E_TURN=server uv run python 13_e2e.py D $v cartesia; done
for v in Laura Abby Ethan; do run 13_e2e_D_$(echo $v | tr A-Z a-z)_fish_server E2E_TURN=server uv run python 13_e2e.py D $v fish; done
run 13_e2e_B_harlow_whisperstt E2E_STT=gpt-realtime-whisper uv run python 13_e2e.py B Harlow
echo "$(date +%T) ALL DONE"
