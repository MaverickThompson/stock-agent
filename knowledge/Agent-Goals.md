# Agent Goals

## Objective

Maximise long-term risk-adjusted returns while minimising catastrophic
mistakes. Not the number of trades, and not the hit rate.

**A missed opportunity is acceptable. A preventable loss is not.** The two are
not symmetric: a missed trade costs the return you did not get, while a
preventable loss costs capital *and* the compounding on that capital for the
rest of the account's life.

## The five agents

### 1. Manager (Supervisor / CIO)
Reviews every decision. Detects conflicts, weak reasoning and missing
information. Rejects poor ideas and sends work back for reconsideration.
Requires evidence from every agent.

Never blindly accepts a recommendation. A majority is explicitly not
sufficient — four agents agreeing on a weak signal is four correlated errors,
not four confirmations. Primary goal: preserving capital.

Review order (first match wins): structural blocks → weak reasoning → missing
information → conflict → insufficient support → approve.

### 2. Market Regime
Determines the regime via HMM: Forward algorithm for filtered posteriors,
Viterbi for the decoded path, Baum-Welch for training. Reports confidence,
persistence and transition risk.

Also **audits its own model**: cross-checks the HMM against a plain EMA-200
trend rule and reports disagreement rather than hiding it. When a moving
average and a five-state hidden Markov model disagree about whether this is a
bull market, that is information.

Regimes: Bull, Bear, Sideways, Recovery, HighVolatility.

### 3. Stock Discovery
Scans the universe and maintains a ranked watchlist. Momentum, trend structure,
unusual volume, technical setup quality, with an extension penalty and a hard
liquidity gate.

Its output is a **screen, not a thesis**. Rank 1 on a momentum screen is also
the name most likely to be extended, which is the Devil's Advocate's opening
argument.

### 4. Risk
Challenges every trade and looks for reasons **not** to trade. Sizes positions,
calculates drawdown exposure, evaluates stops and account exposure, rejects
dangerous opportunities.

**Encouraged to disagree.** Holds a veto. Hardens rather than softens when peer
agents are enthusiastic — enthusiasm is not evidence.

### 5. Devil's Advocate
Argues against the conclusions of every other agent. Searches for flaws, hidden
risks, contradictory evidence and conditions that would invalidate the
assumptions. Stress-tests recommendations.

Its findings are almost always *against*. That is the design. Its most valuable
output is the **falsification trigger**: a specific, observable condition that
would prove the thesis wrong. A position with no falsification condition is one
you will still be holding, and rationalising, at −40%.

## Decision process

1. Agents 2–5 analyse **independently** — no agent sees another's work.
2. Conclusions are compared and disagreements identified.
3. Agents debate: each is shown the others' reports and may rebut.
4. Agents revise.
5. The Manager reviews everything.
6. The Manager returns APPROVE / REJECT / REQUEST_REANALYSIS.
7. Only approved decisions become recommendations.

Step 1 matters more than it looks. If agents saw each other's work while
forming a first view, they would anchor on whoever ran first, and the debate
would measure ordering rather than evidence.

`REQUEST_REANALYSIS` loops back to step 3. On the final permitted round the
Manager's escalation path hardens from "ask again" to "reject" — an unresolved
question is a no, and a process that can defer forever never has to be right.

## Output contract

Every decision carries:

- **Recommendation** — BUY / SELL / HOLD (only an approved verdict is actionable)
- **Confidence score** — sceptic-weighted; Risk 1.5×, Devil's Advocate 1.2×,
  Regime 1.0×, Discovery 0.8×, less 3% per unresolved objection
- **Risk level** — position size, stop, targets, worst-case loss in currency
  and as a percentage of equity
- **Explanation** — the full debate record, every finding with its evidence,
  written to `logs/decisions.jsonl`

## Standing constraints

- These agents are **deterministic and quantitative**, not LLM calls. The
  debate has to be reproducible and auditable, and a backtest over 5,000 bars
  cannot make 25,000 model calls. `Agent` defines the interface an LLM-backed
  reasoner would implement; see `prompts/master_prompt.txt`.
- No agent places an order. Execution is manual and human.
- Data quality warnings are surfaced as findings, not footnotes.
- When the model is structurally blind — no fitted state resembles a Bear
  market, say — that is reported as a *serious* finding. Silence about a risk
  is absence of evidence, not evidence of absence.
