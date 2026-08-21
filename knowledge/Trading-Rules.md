# Trading Rules

These are the standing rules the system enforces in code, not aspirations. Each
one names the config field that implements it, so a rule cannot drift away from
its enforcement.

A rule with a blank threshold is not a rule. Every value below is set.

## Capital preservation

| Rule | Value | Enforced by |
|---|---|---|
| Never risk more than 1% of equity on a single trade | `risk.max_risk_per_trade = 0.01` | `Portfolio.size` |
| Total open risk across all positions capped | `risk.max_portfolio_heat = 0.06` | `Portfolio.size`, `RiskAgent` |
| Maximum open positions | `risk.max_open_positions = 10` | `Portfolio.size` |
| No leverage — position cost cannot exceed cash | — | `Portfolio.open` |
| Minimum liquidity, 20-day median dollar volume | `risk.min_dollar_volume = 5_000_000` | `RiskAgent`, `DiscoveryAgent` |

The heat cap is the rule that actually matters. Ten positions each risking 1%
is not a 1% risk; in a correction, correlations converge toward 1 and they all
stop out together. 6% is the real ceiling on a bad day.

## Position entry

- Always calculate the stop **before** sizing. Size is derived from the stop
  distance, never the other way round.
- Stop distance is `atr_stop_multiple` × ATR(14), default 2.0 — not a round
  percentage. A fixed 5% stop is too tight for a volatile name and too loose
  for a quiet one.
- Shares = `floor(risk_budget / |entry - stop|)`. If that rounds to zero, there
  is no trade.
- Reward-to-risk to the first target must be at least
  `risk.min_reward_risk = 1.5`.
- No new entries during a news blackout — earnings, FOMC, CPI
  (`risk.news_blackout_days = 1`). Set `live["news_blackout"]` to activate.
- No new long exposure when the regime is `Bear` or `HighVolatility` at ≥50%
  confidence. This is a `BLOCKING` finding, not a preference.

## Position exit

- Take-profit targets at `risk.target_r_multiples = (1.5, 3.0)` — multiples of
  the initial risk R.
- The stop is placed with the broker at entry, not held mentally.
- Every position carries a **falsification trigger** written before entry (the
  Devil's Advocate generates one). If it fires, exit — the thesis was wrong,
  which is different from the position being temporarily down.

## Behavioural guardrails

- The system is not trying to maximise the number of trades. A missed
  opportunity is acceptable; a preventable loss is not.
- A majority vote among agents is not sufficient. The Manager reviews every
  decision and the Risk and Devil's Advocate agents each hold a veto.
- Unanimity at high confidence is treated as a *warning* about the process, not
  a confirmation of the conclusion.
- Claims without evidence do not count toward consensus and are grounds for
  sending the decision back.

## Review cadence

- Re-run `analyze` on open positions at least weekly.
- Refit the HMM at least quarterly (`backtest.refit_every = 63` bars is the
  backtest cadence; live, refit whenever you re-run).
- **Pre-committed review point:** at 50 closed trades, compare cumulative
  excess return against SPY. If it is negative, move 80% to a passive index.
  This is decided now, in advance, precisely so it cannot be renegotiated later
  by someone holding a losing hand.

## Execution

All orders are placed **manually**, by a human, in the broker. Nothing in this
repository connects to a brokerage or transmits an order. The system produces
analysis and labelled recommendations; the decision to act is yours and happens
outside it.
