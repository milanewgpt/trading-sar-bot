#!/bin/bash
set -e

DATA_DIR="${DATA_DIR:-.}"
mkdir -p "$DATA_DIR"
chmod 755 "$DATA_DIR" 2>/dev/null || true

TRADES="$DATA_DIR/trades.csv"
if [ ! -f "$TRADES" ]; then
  cat > "$TRADES" <<'CSV'
mode,strategy,timestamp,symbol,direction,entry,stop,take,quantity,margin,close_reason,result
PAPER,EMA,2026-05-05T14:00:30.898977+00:00,SOL-USDT,SHORT,84.104,86.03533399999999,80.24133200000001,1,1.0,SL,LOSS -1.93$
PAPER,EMA,2026-05-08T18:00:25.947533+00:00,SOL-USDT,LONG,89.099,87.69591226321039,91.90517547357923,1,1.0,TP,WIN +2.81$
LIVE,EMA,2026-05-10T17:30:52.534739+00:00,SOL-USDT,LONG,93.72,92.5752741774676,96.00945164506479,1,10.0,TP,WIN +2.29$
LIVE,SAR,2026-05-12T17:45:07.951273+00:00,DOGE-USDT,SHORT,0.10814,0.10918,0.10606,925,10.0,CLOSED,LOSS -0.96$
LIVE,SAR,2026-05-14T20:27:53.811214+00:00,DOGE-USDT,LONG,0.11623,0.11499,0.11871000000000001,860,10.0,CLOSED,LOSS -1.07$
LIVE,SAR,2026-05-15T16:29:38.030592+00:00,DOGE-USDT,SHORT,0.11136,0.11259,0.10890000000000001,898,10.0,CLOSED,LOSS -1.10$
LIVE,EMA,2026-05-20T14:53:44.035622+00:00,SOL-USDT,SHORT,84.634,85.98919599999999,81.92360800000002,1,10.0,CLOSED,LOSS -1.36$
LIVE,SAR,2026-05-23T07:50:59.163475+00:00,DOGE-USDT,SHORT,0.09972,0.10095,0.09813000000000001,1000,10.0,TP,WIN +1.82$
CSV
else
  python3 - "$TRADES" <<'PYEOF'
import csv, sys

path = sys.argv[1]
with open(path, newline="") as f:
    rows = list(csv.DictReader(f))
if not rows:
    sys.exit()

fields = list(rows[0].keys())
changed = False

# Remove duplicate: seed-injected May-23 10:50 entry (bot already logged 07:50)
before = len(rows)
rows = [r for r in rows if r.get("timestamp") != "2026-05-23T10:50:00.000000+00:00"]
if len(rows) < before:
    changed = True
    print("migration: removed duplicate May-23 10:50 SAR entry")

# Fix any remaining CLOSED May-23 SAR entries → TP WIN
for r in rows:
    if (r.get("timestamp", "").startswith("2026-05-23")
            and r.get("strategy") == "SAR"
            and r.get("close_reason") == "CLOSED"):
        r["entry"] = "0.09972"
        r["close_reason"] = "TP"
        r["result"] = "WIN +1.82$"
        changed = True
        print("migration: fixed May-23 SAR → TP WIN +1.82$")

# Add missing EMA May-20 LOSS if absent
existing_ts = {r.get("timestamp", "") for r in rows}
to_add = [
    {"mode":"LIVE","strategy":"EMA","timestamp":"2026-05-20T14:53:44.035622+00:00",
     "symbol":"SOL-USDT","direction":"SHORT","entry":"84.634",
     "stop":"85.98919599999999","take":"81.92360800000002",
     "quantity":"1","margin":"10.0","close_reason":"CLOSED","result":"LOSS -1.36$"},
    {"mode":"LIVE","strategy":"SAR","timestamp":"2026-05-23T07:50:59.163475+00:00",
     "symbol":"DOGE-USDT","direction":"SHORT","entry":"0.09972",
     "stop":"0.10095","take":"0.09813000000000001",
     "quantity":"1000","margin":"10.0","close_reason":"TP","result":"WIN +1.82$"},
]
for row in to_add:
    if row["timestamp"] not in existing_ts:
        rows.append(row)
        existing_ts.add(row["timestamp"])
        changed = True
        print(f"migration: added {row['timestamp']} {row['strategy']}")

# Fix EMA SOL SHORT June-03: TP was incorrectly logged as CLOSED/LOSS (fillHistory bug)
# Actual close: BUY SHORT at 72.595, realizedPNL = +3.473
for r in rows:
    if (r.get("mode") == "LIVE"
            and r.get("strategy") == "EMA"
            and r.get("symbol") == "SOL-USDT"
            and r.get("direction") == "SHORT"
            and str(r.get("entry","")).startswith("76.")
            and r.get("timestamp","").startswith("2026-06")
            and "LOSS" in r.get("result","")):
        r["close_reason"] = "TP"
        r["result"] = "WIN +3.47$"
        changed = True
        print("migration: fixed EMA SOL SHORT Jun-03 CLOSED→TP WIN +3.47$")

# Fix SAR DOGE SHORT June-03/04: TP was incorrectly logged as CLOSED/LOSS -0.84$
# Actual close: BUY SHORT at 0.09145, realizedPNL = +1.677
for r in rows:
    if (r.get("mode") == "LIVE"
            and r.get("strategy") == "SAR"
            and r.get("symbol") == "DOGE-USDT"
            and r.get("direction") == "SHORT"
            and str(r.get("entry","")).startswith("0.0930")
            and r.get("timestamp","").startswith("2026-06")
            and "LOSS" in r.get("result","")):
        r["close_reason"] = "TP"
        r["result"] = "WIN +1.68$"
        changed = True
        print("migration: fixed SAR DOGE SHORT Jun-03 CLOSED→TP WIN +1.68$")

# Deduplicate paper trades: for same strategy+symbol+direction+entry, keep only first occurrence
seen = {}
deduped = []
for r in rows:
    key = (r.get("strategy"), r.get("symbol"), r.get("direction"), r.get("entry"))
    if r.get("mode") == "PAPER" and key in seen:
        changed = True
        print(f"migration: removed duplicate paper trade {r.get('strategy')} {r.get('direction')} @ {r.get('entry')} ts={r.get('timestamp')}")
    else:
        deduped.append(r)
        if r.get("mode") == "PAPER":
            seen[key] = True
rows = deduped

if changed:
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
PYEOF
fi

# Restore SAR_SOL position that was lost due to Markdown notification bug (2026-06-04)
# Trade opened at 68.654 SHORT but state was reset to MONITORING before this fix.
python3 - "$DATA_DIR/state_sar_sol_live.json" <<'PYEOF'
import json, sys, os

path = sys.argv[1]
if not os.path.exists(path):
    sys.exit()

with open(path) as f:
    state = json.load(f)

migrations = state.get("_migrations", [])
if (state.get("state") == "monitoring"
        and state.get("position") is None
        and "sol_short_68654_jun04" not in migrations):
    state["state"] = "position_open"
    state["signal"] = None
    state["_migrations"] = migrations + ["sol_short_68654_jun04"]
    state["position"] = {
        "direction": "short",
        "entry": 68.654,
        "stop": 69.6,
        "take": 66.762,
        "quantity": 1,
        "open_time": "2026-06-04T12:10:00+00:00"
    }
    with open(path, "w") as f:
        json.dump(state, f, indent=2)
    print("migration: restored SAR_SOL SHORT 68.654 -> position_open")
PYEOF

exec python3 bot.py
