#!/usr/bin/env bash
# Resilient poller for the DealMachine enrichment pass.
# DealMachine's API is intermittently down (server-side Prisma DB-pool timeouts).
# The enricher is resumable (checkpoints to dealmachine_results.json, fail-fasts
# when the API is down). This loop re-invokes it until coverage is complete or a
# wall-clock cap is hit, sleeping between rounds so we don't hammer a down API.
set -u
cd "$(dirname "$0")/.."
LOG=/tmp/dm_poll.log
TARGET=454            # unique keys to resolve (427 APN + 27 address)
MAX_ROUNDS=120        # ~ up to 2h at 180s/round
: > "$LOG"
for round in $(seq 1 "$MAX_ROUNDS"); do
  echo "===== round $round @ $(date +%H:%M:%S) =====" >> "$LOG"
  python3 scrapers/dealmachine_enrich.py >> "$LOG" 2>&1
  rc=$?
  done=$(python3 -c "import json;print(len(json.load(open('data/enriched/dealmachine_results.json'))))" 2>/dev/null || echo 0)
  echo "[round $round rc=$rc] resolved=$done/$TARGET" >> "$LOG"
  if [ "$rc" -eq 0 ] || [ "$done" -ge "$TARGET" ]; then
    echo "COMPLETE: rc=$rc coverage=$done/$TARGET" >> "$LOG"; exit 0
  fi
  # rc 3 = API down (health gate); rc 4 = partial. Back off and retry.
  sleep 90
done
echo "MAXROUNDS: stopped at $done/$TARGET after $MAX_ROUNDS rounds" >> "$LOG"
exit 2
