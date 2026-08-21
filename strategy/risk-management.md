# Risk Management

## Fixed-fractional sizing

The rule inverts the usual question. Instead of "how many shares do I want",
it asks "how far is the stop, and how many shares make that distance cost
exactly 1% of equity".

```
risk_budget    = equity × max_risk_per_trade      # 1%
risk_per_share = |entry − stop|                   # 2 × ATR(14)
shares         = floor(risk_budget / risk_per_share)
```

The consequence is that **a wider stop buys fewer shares**. Every position
risks the same amount regardless of how volatile the instrument is, which is
what makes trades comparable to each other and R-multiples meaningful.

### Worked example

Equity $10,000. SPY at $773.26, ATR(14) = $8.94.

```
risk_budget    = 10,000 × 0.01           = $100.00
stop           = 773.26 − 2 × 8.94       = $755.38
risk_per_share = 773.26 − 755.38         = $17.88
shares         = floor(100.00 / 17.88)   = 5
actual risk    = 5 × 17.88               = $89.40  (0.89% of equity)
targets        = 773.26 + 17.88 × (1.5, 3.0) = $800.08, $826.90
reward:risk    = 1.5 to first target
```

Note the position *cost* is 5 × $773.26 = $3,866 — 39% of the account — while
the *risk* is $89. Cost and risk are different quantities, and confusing them
is the most common sizing error.

## Portfolio heat

Heat is the sum of open risk across all positions, as a fraction of equity.

```
heat = Σ(shares_i × |entry_i − stop_i|) / equity
```

Capped at 6%. The per-trade cap alone is not enough: ten independent 1% risks
is a 1% expected worst day only if they are actually independent. In a real
correction, equity correlations converge toward 1 and every stop fills on the
same morning. The heat cap is what bounds that scenario.

`Portfolio.size` takes the *smaller* of the per-trade budget and the remaining
heat, so the 8th position is automatically smaller than the 1st.

## Correlation

The Risk Agent computes 126-day return correlation between a candidate and
every open position. Above 0.70 it raises a `CONCERN`; above 0.85, `SERIOUS`.

Buying QQQ while holding SPY is not diversification. It is the same bet, sized
twice, with the paperwork of a diversified book.

## Drawdown arithmetic

Recovery is not symmetric with loss, which is the entire argument for stops:

| Drawdown | Gain needed to recover |
|---|---|
| −10% | +11% |
| −20% | +25% |
| −33% | +50% |
| −50% | +100% |
| −75% | +300% |

At 1% risk per trade, twenty consecutive losses — far worse than any realistic
losing streak — costs about 18% of the account. That is survivable. The rules
exist to keep the left tail bounded, not to raise the average.

## Regime-conditional exposure

Baseline exposure by regime, before volatility targeting:

| Regime | Baseline | Reasoning |
|---|---|---|
| Bull | 100% | Positive drift, contained volatility |
| Recovery | 50% | Right direction, wrong volatility; frequently a head-fake |
| Sideways | 35% | No reliable drift; breakouts mostly fail |
| HighVolatility | 15% | Direction unresolved |
| Bear | 0% | Capital preservation |

Each baseline is then scaled by `clip(0.15 / state_volatility, 0.25, 1.0)`.
Three states can all legitimately be `Bull` while behaving very differently —
+24%/yr at 7% volatility is not the same trade as +12%/yr at 19%. Volatility
targeting equalises the *risk* each contributes rather than treating one label
as one position size.

## What this does not manage

Stated plainly, because unlisted risks are the ones that hurt:

- **Gap risk.** A stop is not a guarantee. Overnight gaps and halts fill below
  the stop. Sizing assumes the stop fills at the stop; sometimes it does not.
- **Taxes.** Not modelled anywhere, including in the backtest. Short-term gains
  are taxed as ordinary income, which can erase a thin edge entirely.
- **Slippage beyond 5bps.** The backtest assumes 5bps per unit of turnover.
  Illiquid names in fast markets cost far more.
- **Correlation regime shift.** Historical correlation understates crisis
  correlation, systematically and in the worst possible direction.
- **Model risk.** The HMM is fitted from a finite sample and its states are one
  of several local optima found by Baum-Welch. The Devil's Advocate reports the
  restart spread precisely because this is a real risk, not a footnote.
