"""Turning anonymous HMM states into named market regimes.

Baum-Welch returns states numbered 0..K-1 with no meaning attached. State 2 in
one refit may be state 4 in the next. Before anything downstream can say "we are
in a bear market", each state has to be matched to a regime *by its behaviour*.

Matching uses **absolute** anchors, not a ranking
--------------------------------------------------
An earlier version of this module solved a one-to-one assignment (Hungarian) of
states to archetypes in cross-state z-score space. It produced a nonsense
result on real data: trained on SPY 2006-2020, it labelled a state returning
**+0.4%/yr** as ``Bear``, purely because it was the *least bullish* of five
states and the bijection demanded that something be called Bear.

That is the failure mode this whole project exists to avoid -- a confident,
well-formatted, wrong answer. Two changes fix it:

1. Archetypes live in interpretable absolute units (annualised return and
   volatility), so "Bear" means *actually losing money*, not "relatively worst".
2. Each state independently takes its nearest archetype. Labels may repeat and
   labels may go unused. A long bull sample legitimately contains three shades
   of Bull and no Bear, and :meth:`RegimeMap.warnings` says so out loud -- a
   model that has never seen a bear market cannot warn you about one.

Coordinates are normalised as ``r = ann_return / 0.15`` and
``v = (ann_vol - 0.16) / 0.10``, so 1.0 on the return axis is +15%/yr and 1.0 on
the volatility axis is 26%/yr realised.

======================  ========  ==========  ==================================
Regime                  Return    Volatility  Reads as
======================  ========  ==========  ==================================
BULL                    +15%/yr      12%      grinding higher, calm
BEAR                    -20%/yr      24%      actually losing money, agitated
SIDEWAYS                  0%/yr      13%      going nowhere, quiet
RECOVERY                +22%/yr      26%      snapping back, still violent
HIGH_VOLATILITY          -5%/yr      34%      direction unclear, tape wild
======================  ========  ==========  ==================================

RECOVERY and BULL are both positive-return states; volatility separates them.
The bounce off a crash low and a quiet uptrend have very different survival
characteristics, and sizing a position identically in both is how accounts get
destroyed.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..logging_setup import get_logger
from .hmm_model import RegimeHMM

LOG = get_logger("regime.labeling")

#: Fallback anchors, calibrated on a broad equity index (~16%/yr volatility).
#: Used only when the instrument's own volatility cannot be estimated.
RETURN_SCALE = 0.15
VOL_CENTER = 0.16
VOL_SCALE = 0.10

#: The instrument's own volatility is converted into anchors with these ratios.
#: They are set so a 16.7%-volatility index reproduces the constants above,
#: which keeps index behaviour unchanged while letting wilder instruments scale.
RETURN_SCALE_RATIO = 0.90
VOL_SCALE_RATIO = 0.625

#: A state cannot be Recovery or HighVolatility unless its volatility clears
#: both its instrument's baseline *and* this absolute floor. "High volatility"
#: has an absolute meaning: a 12%-volatility state is calm no matter how
#: sleepy the rest of the instrument is.
HIGH_VOL_ABS_FLOOR = 0.18

#: A state cannot be labelled Bear if it made money. This is an absolute guard
#: that no amount of rescaling may override -- see the module docstring.
BEAR_MAX_RETURN = 0.02

#: Floor on the estimated baseline volatility, to stop a near-constant series
#: producing an infinitely sensitive scale.
MIN_BASE_VOL = 0.05

#: Annualised volatility the exposure scalar targets.
TARGET_VOL = 0.15

#: How far a state may sit from every archetype before we admit we do not
#: recognise it, in normalised units.
UNRECOGNISED_DISTANCE = 2.5


class RegimeLabel(str, enum.Enum):
    """Named market regimes."""

    BULL = "Bull"
    BEAR = "Bear"
    SIDEWAYS = "Sideways"
    RECOVERY = "Recovery"
    HIGH_VOLATILITY = "HighVolatility"

    @property
    def is_risk_on(self) -> bool:
        return self in (RegimeLabel.BULL, RegimeLabel.RECOVERY)

    @property
    def target_exposure(self) -> float:
        """Baseline equity exposure for this regime, in [0, 1].

        A starting point for the Risk Agent to argue with, not an instruction.
        RECOVERY is deliberately capped below BULL: recovery states have the
        right sign and the wrong volatility, and they are frequently head-fakes.
        """
        return {
            RegimeLabel.BULL: 1.00,
            RegimeLabel.RECOVERY: 0.50,
            RegimeLabel.SIDEWAYS: 0.35,
            RegimeLabel.HIGH_VOLATILITY: 0.15,
            RegimeLabel.BEAR: 0.00,
        }[self]

    @property
    def description(self) -> str:
        return {
            RegimeLabel.BULL: "Positive drift, contained volatility. Trend-following works.",
            RegimeLabel.BEAR: "Negative drift with elevated volatility. Capital preservation.",
            RegimeLabel.SIDEWAYS: "No reliable drift, low volatility. Breakouts mostly fail.",
            RegimeLabel.RECOVERY: "Positive drift but volatility still elevated. Size small.",
            RegimeLabel.HIGH_VOLATILITY: "Direction unresolved, volatility extreme. Stand aside.",
        }[self]


#: Archetype coordinates in (normalised return, normalised volatility).
ARCHETYPES: dict[RegimeLabel, tuple[float, float]] = {
    RegimeLabel.BULL: (1.0, -0.4),
    RegimeLabel.BEAR: (-1.3, 0.8),
    RegimeLabel.SIDEWAYS: (0.0, -0.3),
    RegimeLabel.RECOVERY: (1.5, 1.0),
    RegimeLabel.HIGH_VOLATILITY: (-0.3, 1.8),
}

#: Which archetypes may be used for a given state count. With only two or three
#: states the model cannot support the Recovery / HighVolatility distinction,
#: so those archetypes are withheld rather than being assigned spuriously.
LABEL_SETS: dict[int, list[RegimeLabel]] = {
    2: [RegimeLabel.BULL, RegimeLabel.BEAR],
    3: [RegimeLabel.BULL, RegimeLabel.BEAR, RegimeLabel.SIDEWAYS],
    4: [RegimeLabel.BULL, RegimeLabel.BEAR, RegimeLabel.SIDEWAYS,
        RegimeLabel.HIGH_VOLATILITY],
    5: list(ARCHETYPES),
    6: list(ARCHETYPES),
}


@dataclass(frozen=True)
class ArchetypeScale:
    """Per-instrument normalisation for the archetype space.

    Fixed anchors calibrated on an index mislabel individual stocks. Running
    the five-state model on ERIC produced a state at −59.5%/yr and 71.2%
    volatility, which sat 5.2 units from *every* archetype and was reported as
    an unrecognised HighVolatility. It is obviously a bear regime; it only
    looked alien because the anchors assumed SPY-like volatility.

    Scaling the axes by the instrument's own baseline volatility fixes that:
    the same state normalises to (−1.8, +1.7), lands on Bear at distance 1.0,
    and is recognised. A −20%/yr state at 24% volatility and a −60%/yr state at
    71% volatility have nearly identical Sharpe ratios; they are the same
    regime seen through instruments of different amplitude.

    What does *not* rescale is the meaning of the labels. Bear still requires
    actually losing money and Recovery/HighVolatility still require absolutely
    high volatility, both enforced as gates in :func:`_assign_labels`.
    """

    base_vol: float
    return_scale: float
    vol_center: float
    vol_scale: float
    #: True when the scale came from the instrument rather than the fallback.
    calibrated: bool = True

    @classmethod
    def from_states(cls, raw: dict[int, dict[str, float]]) -> "ArchetypeScale":
        """Estimate the baseline from the fitted states' own behaviour.

        Frequency-weighted mean of state volatilities: the volatility the
        instrument spends most of its time at, rather than an unconditional
        standard deviation that one crisis can dominate.
        """
        vols = [(r["vol"], r["freq"]) for r in raw.values()
                if not np.isnan(r["vol"]) and r["freq"] > 0]
        total_weight = sum(freq for _, freq in vols)
        if not vols or total_weight <= 0:
            return cls(VOL_CENTER, RETURN_SCALE, VOL_CENTER, VOL_SCALE,
                       calibrated=False)

        base = sum(vol * freq for vol, freq in vols) / total_weight
        base = max(float(base), MIN_BASE_VOL)
        return cls(
            base_vol=base,
            return_scale=base * RETURN_SCALE_RATIO,
            vol_center=base,
            vol_scale=base * VOL_SCALE_RATIO,
        )

    @classmethod
    def index_default(cls) -> "ArchetypeScale":
        return cls(VOL_CENTER, RETURN_SCALE, VOL_CENTER, VOL_SCALE, calibrated=False)

    def normalise(self, ann_return: float, ann_vol: float) -> np.ndarray:
        return np.array([
            ann_return / self.return_scale,
            (ann_vol - self.vol_center) / self.vol_scale,
        ])

    @property
    def high_vol_threshold(self) -> float:
        """Volatility a state must clear to be Recovery or HighVolatility."""
        return max(HIGH_VOL_ABS_FLOOR, self.base_vol)


@dataclass(frozen=True)
class StateStats:
    """Empirical behaviour of one hidden state, in interpretable units."""

    state: int
    label: RegimeLabel
    #: Mean daily return while in this state, annualised (252d).
    ann_return: float
    #: Standard deviation of daily returns while in this state, annualised.
    ann_volatility: float
    #: Share of training bars assigned to this state.
    frequency: float
    #: Mean run length in bars, measured from the decoded path.
    mean_duration: float
    #: Model-implied persistence, 1/(1 - a_ii). Compare with mean_duration:
    #: a large gap means the fit does not describe the data it was trained on.
    implied_duration: float
    n_bars: int
    #: Distance to the archetype it was matched to, in normalised units.
    match_distance: float

    @property
    def sharpe(self) -> float:
        """Annualised return over annualised volatility. Not risk-free adjusted."""
        if self.ann_volatility <= 0:
            return 0.0
        return self.ann_return / self.ann_volatility

    @property
    def is_recognised(self) -> bool:
        """False when the state resembles no archetype closely enough."""
        return self.match_distance <= UNRECOGNISED_DISTANCE

    @property
    def exposure(self) -> float:
        """Suggested exposure: the label's baseline, volatility-targeted.

        Three states can all legitimately be ``Bull`` while behaving very
        differently -- +22%/yr at 7% volatility is not the same trade as
        +9%/yr at 15%. Scaling the baseline by ``TARGET_VOL / state_vol``
        keeps the *risk* contributed by each roughly equal instead of treating
        one label as one position size.
        """
        base = self.label.target_exposure
        if base <= 0.0 or self.ann_volatility <= 0.0:
            return base
        scalar = float(np.clip(TARGET_VOL / self.ann_volatility, 0.25, 1.0))
        return round(base * scalar, 4)

    def summary(self) -> str:
        flag = "" if self.is_recognised else "  [UNRECOGNISED]"
        return (
            f"state {self.state} -> {self.label.value}: "
            f"return {self.ann_return:+.1%}/yr, vol {self.ann_volatility:.1%}/yr, "
            f"{self.frequency:.0%} of bars, ~{self.mean_duration:.0f}d runs, "
            f"exposure {self.exposure:.0%}{flag}"
        )


@dataclass
class RegimeMap:
    """The full state -> regime mapping plus the evidence behind it."""

    stats: dict[int, StateStats]
    n_states: int
    #: The per-instrument normalisation the labels were assigned under.
    scale: ArchetypeScale = field(default_factory=ArchetypeScale.index_default)

    def label_of(self, state: int) -> RegimeLabel:
        return self.stats[int(state)].label

    def labels(self) -> list[RegimeLabel]:
        """Labels ordered by state index, for indexing probability columns."""
        return [self.stats[i].label for i in range(self.n_states)]

    def represented(self) -> set[RegimeLabel]:
        """Regimes that at least one state actually matched."""
        return {s.label for s in self.stats.values()}

    def missing(self) -> list[RegimeLabel]:
        """Archetypes eligible for this state count that no state matched."""
        eligible = LABEL_SETS.get(self.n_states, LABEL_SETS[5])
        return [label for label in eligible if label not in self.represented()]

    def probability_by_label(self, probs: np.ndarray) -> dict[RegimeLabel, float]:
        """Collapse a state-probability row onto regime names.

        Labels repeat by design, so probabilities are summed, not overwritten.
        """
        out: dict[RegimeLabel, float] = {}
        for state, prob in enumerate(np.asarray(probs, dtype=float).ravel()):
            label = self.label_of(state)
            out[label] = out.get(label, 0.0) + float(prob)
        return out

    def expected_exposure(self, probs: np.ndarray) -> float:
        """Probability-weighted exposure across states.

        Averaging over the posterior rather than committing to ``argmax`` means
        a 40/35/25 split across three regimes sizes like the genuinely uncertain
        situation it is, instead of like a confident call on the plurality.
        """
        probs = np.asarray(probs, dtype=float).ravel()
        return float(sum(probs[s] * self.stats[s].exposure for s in range(self.n_states)))

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "state": s.state, "label": s.label.value,
                    "ann_return": s.ann_return, "ann_volatility": s.ann_volatility,
                    "sharpe": s.sharpe, "frequency": s.frequency,
                    "mean_duration": s.mean_duration,
                    "implied_duration": s.implied_duration,
                    "exposure": s.exposure, "match_distance": s.match_distance,
                    "n_bars": s.n_bars,
                }
                for s in sorted(self.stats.values(), key=lambda x: x.state)
            ]
        ).set_index("state")

    def warnings(self) -> list[str]:
        """Signs that the fitted states are not describing real regimes."""
        out: list[str] = []
        for s in sorted(self.stats.values(), key=lambda x: x.state):
            if s.mean_duration < 3:
                out.append(
                    f"{s.label.value} (state {s.state}) lasts ~{s.mean_duration:.1f} bars "
                    "on average; that is noise being labelled as a regime"
                )
            if s.frequency < 0.02:
                out.append(
                    f"{s.label.value} (state {s.state}) covers only {s.frequency:.1%} of "
                    f"history ({s.n_bars} bars); its parameters are barely estimated"
                )
            if not s.is_recognised:
                out.append(
                    f"state {s.state} ({s.ann_return:+.1%}/yr, {s.ann_volatility:.1%} vol) "
                    f"sits {s.match_distance:.1f} units from every archetype; the "
                    f"{s.label.value} label is a weak fit"
                )
        for label in self.missing():
            out.append(
                f"no state resembles {label.value}. The training window contains no "
                f"such regime, so this model cannot ever signal one -- treat its "
                f"silence about {label.value} as absence of evidence, not evidence "
                "of absence"
            )
        return out


def build_regime_map(model: RegimeHMM, returns: pd.Series, X: np.ndarray,
                     *, use_viterbi: bool = True) -> RegimeMap:
    """Assign regime names to a fitted model's states.

    Parameters
    ----------
    model:
        A fitted :class:`RegimeHMM`.
    returns:
        Daily log returns aligned row-for-row with ``X``.
    X:
        The feature matrix the model was fitted on.
    use_viterbi:
        Decode with Viterbi (default). This runs over the *training* window to
        characterise states, where using the whole sequence is correct -- we are
        describing history, not generating a signal.
    """
    returns = pd.Series(returns).astype(float)
    if len(returns) != len(X):
        raise ValueError(
            f"returns has {len(returns)} rows but X has {len(X)}; they must be aligned"
        )

    path = model.viterbi(X) if use_viterbi else model.smooth(X).argmax(axis=1)
    implied = model.expected_duration()
    n_states = model.n_states

    raw: dict[int, dict[str, float]] = {}
    for state in range(n_states):
        mask = path == state
        n_bars = int(mask.sum())
        if n_bars < 2:
            # Unvisited state: nothing empirical to describe it with. Mark it
            # as maximally unrecognised rather than inventing statistics.
            raw[state] = {"mean": 0.0, "vol": float("nan"), "freq": 0.0,
                          "dur": 0.0, "n": n_bars}
            continue
        sample = returns.to_numpy()[mask]
        raw[state] = {
            "mean": float(np.mean(sample)) * 252.0,
            "vol": float(np.std(sample, ddof=0)) * np.sqrt(252.0),
            "freq": n_bars / len(path),
            "dur": _mean_run_length(path, state),
            "n": n_bars,
        }

    scale = ArchetypeScale.from_states(raw)
    assignments = _assign_labels(raw, n_states, scale)
    stats = {
        state: StateStats(
            state=state, label=label,
            ann_return=raw[state]["mean"],
            ann_volatility=0.0 if np.isnan(raw[state]["vol"]) else raw[state]["vol"],
            frequency=raw[state]["freq"], mean_duration=raw[state]["dur"],
            implied_duration=float(implied[state]), n_bars=int(raw[state]["n"]),
            match_distance=distance,
        )
        for state, (label, distance) in assignments.items()
    }

    regime_map = RegimeMap(stats=stats, n_states=n_states, scale=scale)
    LOG.info("archetypes calibrated to baseline volatility %.1f%% "
             "(high-vol regimes require >= %.1f%%)",
             scale.base_vol * 100, scale.high_vol_threshold * 100)
    for state in range(n_states):
        LOG.info("%s", stats[state].summary())
    for warning in regime_map.warnings():
        LOG.warning("regime map: %s", warning)
    return regime_map


#: Archetypes that require absolutely-high volatility, not merely high for
#: this instrument.
_HIGH_VOL_LABELS = (RegimeLabel.RECOVERY, RegimeLabel.HIGH_VOLATILITY)


def _assign_labels(raw: dict[int, dict[str, float]], n_states: int,
                   scale: ArchetypeScale | None = None
                   ) -> dict[int, tuple[RegimeLabel, float]]:
    """Match each state to its nearest *eligible* archetype, independently.

    No bijection: labels may repeat and archetypes may go unused. Two gates run
    before the distance comparison, and they are what stop rescaling from
    eroding the meaning of a label:

    * **Bear** is withheld from any state that made more than
      ``BEAR_MAX_RETURN``. A profitable state is not a bear market, however
      poorly it compares with its peers.
    * **Recovery** and **HighVolatility** are withheld from states quieter than
      ``scale.high_vol_threshold``. Without this, a sleepy instrument's
      slightly-choppier state gets called a crash.

    Returns ``{state: (label, distance)}``.
    """
    scale = scale or ArchetypeScale.from_states(raw)
    all_candidates = LABEL_SETS.get(n_states, LABEL_SETS[5])

    out: dict[int, tuple[RegimeLabel, float]] = {}
    for state in range(n_states):
        vol, mean = raw[state]["vol"], raw[state]["mean"]
        if np.isnan(vol):
            # Never visited: park it in Sideways at zero exposure influence and
            # flag it as unrecognised via a deliberately large distance.
            out[state] = (RegimeLabel.SIDEWAYS, float("inf"))
            continue

        eligible = [
            label for label in all_candidates
            if not (label is RegimeLabel.BEAR and mean > BEAR_MAX_RETURN)
            and not (label in _HIGH_VOL_LABELS and vol < scale.high_vol_threshold)
        ]
        if not eligible:  # every archetype gated out; Sideways is the residual
            out[state] = (RegimeLabel.SIDEWAYS, float("inf"))
            continue

        point = scale.normalise(mean, vol)
        targets = np.array([ARCHETYPES[label] for label in eligible])
        distances = np.linalg.norm(targets - point, axis=1)
        best = int(np.argmin(distances))
        out[state] = (eligible[best], float(distances[best]))
    return out


def _mean_run_length(path: np.ndarray, state: int) -> float:
    """Average number of consecutive bars spent in ``state`` per visit."""
    mask = np.asarray(path) == state
    if not mask.any():
        return 0.0
    # Count transitions into the state; each one starts a run.
    entries = int(np.sum(mask[1:] & ~mask[:-1])) + int(mask[0])
    return float(mask.sum() / max(entries, 1))
