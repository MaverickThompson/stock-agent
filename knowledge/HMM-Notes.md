# HMM Notes

Working notes on the model, mapped to Jurafsky & Martin, *Speech and Language
Processing* (3rd ed.), Appendix A — the chapter supplied as `hedge fund.pdf`.

## The three problems

An HMM is `λ = (A, B, π)`: transition matrix `A`, emission distributions `B`,
initial distribution `π`. Hidden states `q`, observations `o`.

| # | Problem | Algorithm | Code | Appendix |
|---|---|---|---|---|
| 1 | Likelihood: `P(O | λ)` | Forward | `RegimeHMM.forward` | A.3 |
| 2 | Decoding: best `Q` | Viterbi | `RegimeHMM.viterbi` | A.4 |
| 3 | Learning: fit `λ` | Forward-Backward | `RegimeHMM.fit` | A.5 |

### Forward (A.3)

```
α₁(j) = π_j · b_j(o₁)
α_t(j) = [ Σᵢ α_{t-1}(i) · a_ij ] · b_j(o_t)
P(O|λ) = Σⱼ α_T(j)
```

Implemented in log space with `logsumexp`. Normalising `α_t` gives
`P(q_t | o₁..o_t)` — the **filtered** posterior, which depends on no
observation after *t*. This is the only state probability safe to trade.

### Viterbi (A.4)

```
δ₁(j) = π_j · b_j(o₁)
δ_t(j) = maxᵢ [ δ_{t-1}(i) · a_ij ] · b_j(o_t)
ψ_t(j) = argmaxᵢ [ δ_{t-1}(i) · a_ij ]
```

Identical to Forward except it takes `max` instead of `Σ`, and keeps
backpointers. J&M make this point explicitly and it is the whole difference:
Forward asks "how likely is this sequence of observations", Viterbi asks "which
state path best explains it".

Viterbi is a **whole-sequence** estimate. The state it assigns to bar *t* can
change when later bars arrive, so it describes history rather than generating a
live signal.

### Baum-Welch (A.5)

EM over `ξ_t(i,j)` (probability of being in `i` at *t* and `j` at *t+1*) and
`γ_t(j)` (probability of being in `j` at *t*), both built from `α` and `β`:

```
E-step:  γ_t(j) ∝ α_t(j) · β_t(j)
         ξ_t(i,j) ∝ α_t(i) · a_ij · b_j(o_{t+1}) · β_{t+1}(j)
M-step:  â_ij = Σ_t ξ_t(i,j) / Σ_t Σ_k ξ_t(i,k)
         μ̂_j, Σ̂_j = γ-weighted mean and covariance of the observations
```

Delegated to `hmmlearn`'s `GaussianHMM.fit`.

## Adapting a speech model to markets

The textbook examples are discrete (ice cream cones, weather). Markets need
three changes.

**Continuous emissions.** `b_j(o_t)` is a multivariate Gaussian over the
3-channel observation vector, not a lookup table.

**Ergodic, not Bakis.** Speech recognisers usually use left-to-right (Bakis)
topologies — the model progresses through phonemes and never goes back. Markets
are fully ergodic: any regime can follow any other. All transitions stay free.

**Local optima are a first-class concern.** Baum-Welch is EM, so it finds a
local optimum that depends on initialisation. On SPY the restart spread is
**444 nats** — different seeds find materially different models. Mitigations:

- `n_restarts = 12`, keep the highest-likelihood fit
- `FitReport.restart_spread` is reported, and the Devil's Advocate raises it as
  a SERIOUS finding when it exceeds 100 nats
- states are sorted by mean return after fitting, so indices are comparable
  across refits

## Why filtering, not smoothing

`hmmlearn.predict_proba` runs full forward-backward and returns
`P(q_t | o₁..o_T)` — **smoothed**, using the entire series including the future.
It is the right tool for labelling historical regimes and completely wrong as a
signal.

The gap is not subtle. On SPY the maximum absolute difference between filtered
and smoothed posteriors is **0.77** — a state can look near-certain in hindsight
and genuinely ambiguous at the time.

`tests/test_hmm.py::test_filtering_is_causal` asserts that filtering a prefix
equals filtering the full series and slicing. `test_smoothing_is_not_causal`
asserts the opposite for `smooth()`, so the distinction stays real rather than
becoming a comment someone deletes.

## Choosing the state count

`n_states = 5` by default (Bull, Bear, Sideways, Recovery, HighVolatility);
`--states 3` gives the classic Bull/Bear/Sideways.

More states always fit better in-sample — log-likelihood rises monotonically.
The checks that matter are whether the extra states are *real*:

- **Duration.** `expected_duration()` = `1/(1 - a_ii)`. A state persisting ~2
  bars is noise wearing a regime's clothes. `RegimeMap.warnings()` flags under 3.
- **Occupancy.** A state holding under 2% of bars has barely-estimated
  parameters. Also flagged.
- **Agreement.** `mean_duration` (empirical) versus `implied_duration` (from
  `A`). A large gap means the fit does not describe its own training data.

On SPY 2006–2026 with 5 states, empirical and implied durations agree closely
(46.4 vs 46.1, 13.5 vs 13.2, 22.7 vs 22.3), which is a good sign the state
count is supportable.

## What the model does and does not find

**Does:** volatility regimes, accurately. 100% of March 2020 bars and 76 of 85
GFC autumn bars were flagged `HighVolatility`. Realised volatility conditional
on the label is correctly ordered on every symbol tested.

**Does not:** predict returns. See the table at the top of the README — the
return ordering by regime is near-inverted on SPY and correctly signed on QQQ.
Same model, same period, opposite conclusion.

This is the known result for HMMs on equity indices, and it follows from the
features: two of the three channels (`log_vol`, `volume_z`) are volatility
proxies, and the return channel is the noisiest of the three. The model is
being asked to segment on what it can actually see, and what it can see is
volatility.

Using it for **sizing** rather than **timing** is the conclusion the evidence
supports.
