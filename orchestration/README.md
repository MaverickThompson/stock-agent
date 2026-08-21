# Orchestration

`n8n_workflow.json` is importable into n8n (Workflows → Import from File). It
schedules the pipeline, handles errors, and routes decisions to human review.

## Flow

```
Schedule (weekdays 16:30 ET, after the close)
   │
   ├─> Fetch Data ........... python scripts/fetch_data.py
   │      └─ on error ─> Alert + stop. Stale data must never reach a decision.
   │
   ├─> Refresh Live Snapshot . writes data/live_snapshot.json from connectors
   │      └─ on error ─> continue; the overlay is optional and load_live_snapshot
   │                     drops fields that have aged out
   │
   ├─> Regime Scan .......... python -m stockagent regime SPY
   │
   ├─> Watchlist ............ python -m stockagent scan --top 10
   │
   ├─> Analyze (per symbol) . python -m stockagent analyze SYM --json out.json
   │      exit 0 = APPROVED, exit 2 = rejected/hold, 1 = error
   │
   ├─> IF approved ─> Human Review queue  (fully autonomous mode: notify only)
   │   ELSE ────────> Log and stop
   │
   └─> Archive .............. logs/decisions.jsonl -> shared storage
```

## Modes

**Human review (default).** Approved recommendations go to a review queue. A
person places the order. Nothing in this repository connects to a brokerage.

**Autonomous.** The workflow runs end to end and notifies rather than queues.
It still does not place orders — "autonomous" here means unattended *analysis*.
If you wire an execution node onto the end, that is your decision and your
risk, and you should re-read `strategy/risk-management.md` first, particularly
the section on what this system does not manage.

## Shared memory

The agents are stateless per run. Continuity lives in files:

| Path | Contents |
|---|---|
| `logs/decisions.jsonl` | every finding, rebuttal and verdict, append-only |
| `data/live_snapshot.json` | latest connector overlay |
| `artifacts/*.pkl` | fitted HMMs, so a run can reuse a model |
| `data/*.csv` | price history |

`DecisionLog.read_all()` reads the audit trail back. Because each record carries
a `run_id` and an ISO timestamp, "why did the system say that on 2026-03-14" is
answerable from the file rather than reconstructed from memory.

## Error recovery

- **Fetch fails** → alert and stop. Never analyse on stale prices.
- **A refit fails** → `run_backtest` sets exposure to 0 and labels the block
  `NoModel` rather than inheriting the previous model's view. Standing flat is
  the honest response to "no model".
- **An agent raises** → `DebateOrchestrator._safe` converts the crash into an
  abstention, which forces the Manager down its missing-information branch. One
  agent failing must not look like agreement.
- **Live snapshot stale** → quotes and sentiment are dropped, scheduled events
  are kept. A stale VIX quoted as current is worse than no VIX.

## Scheduling notes

Run after the close, not during the session. Every feature is computed from
daily bars; running intraday means the last bar is a partial one, and the HMM
will read a half-formed day as a low-volatility observation.

Refit cadence in the backtest is 63 bars (quarterly). Live, the model refits on
every run, which is cheap at this data size (~10s for 12 restarts on 5,000
bars) and keeps the regime map current.
