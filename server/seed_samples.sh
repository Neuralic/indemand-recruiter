#!/bin/bash
# Run the three simulated candidates through the real engine (STUB voice, no credits) and store them as labelled samples.
# usage: server/seed_samples.sh   (about 30 minutes: three full simulated interviews, one at a time)
cd "$(dirname "$0")/../spike"
PY=.venv312/Scripts/python.exe
run() {  # kind  script  name
  echo "=== $1: $3 ($2)"
  CANDIDATE_NAME="${3%% *}" ./restart_stub.sh
  sleep 15
  PYTHONUTF8=1 env -u PIPECAT_ICE_SERVERS $PY 28_sim_candidate.py "$2" > "out/_seed_$1.stdout" 2>&1
  PYTHONUTF8=1 env -u PIPECAT_ICE_SERVERS $PY ../server/seed_samples.py "$1" "$3" 2>&1 | grep -E "^(strong|borderline|weak):|Error|Traceback" 
}
run strong strong "Taylor Brooks"
run borderline borderline "Jordan Ellis"
run weak vague "Casey Rowan"
echo "=== all done"
