#!/bin/sh
set -eu
while true; do
  python3 -u /app/replicate.py || exit $?
  RUN_ONCE=$(python3 -c 'import json; o=json.load(open("/data/options.json")); print("true" if o.get("mode","archive")=="restore" else str(o.get("run_once",True)).lower())')
  if [ "$RUN_ONCE" = "true" ]; then exit 0; fi
  HOURS=$(python3 -c 'import json; print(int(json.load(open("/data/options.json")).get("interval_hours",24)))')
  sleep "$((HOURS * 3600))"
done