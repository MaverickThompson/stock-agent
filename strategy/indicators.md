# Indicators

All implemented in `src/stockagent/indicators.py` in pandas. TA-Lib is not a
dependency — it needs a C build step that is awkward on Windows and every
function here is a few lines.

**Everything is causal.** The value at bar *t* uses only bars ≤ *t*. Wilder's
smoothing is `ewm(alpha=1/period, adjust=False)`, which matches the recursive
definition in his book and the values TA-Lib reports.

| Indicator | Function | Parameters | Used by |
|---|---|---|---|
| RSI | `rsi` | 14, Wilder | Discovery (quality, extension), Devil's Advocate (base rate) |
| MACD | `macd` | 12 / 26 / 9 | Discovery (quality) |
| EMA | `ema` | 20, 50, 200 | Regime cross-check, Discovery (trend), falsification |
| ATR | `atr` | 14, Wilder | **Stop placement and position sizing** |
| Bollinger | `bollinger` | 20, 2σ, ddof=0 | Volatility context |
| OBV | `obv` | — | Devil's Advocate (divergence) |
| Realised vol | `realized_volatility` | 21d, annualised | **HMM feature** |
| Volume z-score | `rolling_zscore` | 63d on log volume | **HMM feature**, Discovery |

## The three that carry weight

**ATR** is the most load-bearing indicator in the system. It sets the stop
distance, which sets the position size, which determines everything about the
risk of the trade. RSI being 3 points off changes a score; ATR being wrong
changes how much money is at risk.

**Realised volatility (logged)** is an HMM observation channel. Volatility is
bounded below by zero with a long right tail; a Gaussian emission fitted to raw
volatility spends density on impossible negative values and gets dragged by the
tail. Logged, it is close to symmetric.

**Volume z-score** is the third channel. Share volume trends with liquidity
over 20 years, so an absolute level means nothing across decades. Z-scoring log
volume against a trailing 63-day window makes the signal "unusually heavy for
this name, lately".

## Scoring in the Discovery Agent

Composite = 0.35·momentum + 0.30·trend + 0.15·volume + 0.20·quality − 0.25·extension

- **momentum** — `tanh` of the mean of 63/126/252-day returns. Squashed so one
  explosive name cannot dominate the ranking on a single lookback.
- **trend** — share of {above EMA50, above EMA200, EMA50 > EMA200}, mapped to [−1, 1].
- **volume** — volume z-score clipped to ±2. Heavy volume confirms a move;
  extreme volume is often capitulation, so the term saturates.
- **quality** — MACD histogram confirmation plus an RSI term peaking near 60.
- **extension** — penalty for distance above the 200-day EMA and RSI over 70.

Liquidity is a **gate applied first**, not a score term. A brilliant setup you
cannot exit at a sane price is not an opportunity.

## What is deliberately absent

- **Fundamentals.** Earnings and revenue growth are read from
  `ctx.live["fundamentals"]` when an MCP connector supplies them. When nothing
  is supplied the agent says so in its findings rather than silently scoring
  zero and implying the factor was considered.
- **Anything fitted on the full sample.** No indicator is standardised against
  statistics that include its own future.
- **Indicator stacking for its own sake.** Eight indicators computed from one
  price series are not eight independent opinions. RSI, MACD and momentum all
  measure recent direction; agreement among them is mostly arithmetic, not
  confirmation, and the composite weights reflect that.
