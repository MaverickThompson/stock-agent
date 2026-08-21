"""Gaussian Hidden Markov Model for market regime detection.

Implements the three classic HMM problems exactly as laid out in Jurafsky &
Martin, *Speech and Language Processing* (3rd ed.), Appendix A:

===========================  =========================  ======================
Problem                      Algorithm                  Method here
===========================  =========================  ======================
A.3 Likelihood               Forward                    :meth:`filter`
A.4 Decoding                 Viterbi                    :meth:`viterbi`
A.5 Learning                 Forward-Backward           :meth:`fit`
                             (Baum-Welch, an EM method)
===========================  =========================  ======================

Baum-Welch training is delegated to ``hmmlearn``'s ``GaussianHMM.fit``. The
forward, backward and Viterbi recursions are implemented here directly, in log
space, for two reasons:

1. **No lookahead.** ``hmmlearn.predict_proba`` returns *smoothed* posteriors
   ``P(q_t | o_1..o_T)`` from the full forward-backward pass -- they use the
   whole series, including bars after *t*. Used as a trading signal that is
   lookahead bias, and it is the single easiest way to produce a backtest that
   looks brilliant and loses money live. :meth:`filter` returns *filtered*
   posteriors ``P(q_t | o_1..o_t)``, which use only information available at
   the time. :meth:`smooth` is provided for research and labelling, and is
   named and documented so it cannot be reached for by accident.

2. It makes the mapping to the textbook checkable -- see ``tests/`` which
   verifies the Viterbi path and forward log-likelihood against ``hmmlearn``.

Notation follows the appendix: ``alpha`` forward, ``beta`` backward, ``A``
transitions, ``B`` emissions, ``pi`` initial distribution.
"""

from __future__ import annotations

import contextlib
import logging
import pathlib
import pickle
import warnings
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy.special import logsumexp
from scipy.stats import multivariate_normal

from ..config import HMMConfig
from ..logging_setup import get_logger

LOG = get_logger("regime.hmm")

try:  # pragma: no cover - exercised by the import itself
    from hmmlearn.hmm import GaussianHMM
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "hmmlearn is required. Install it with:  pip install hmmlearn"
    ) from exc

_LOG_ZERO = -1e300  # stands in for log(0) without producing NaN in arithmetic


@contextlib.contextmanager
def _quiet_hmmlearn():
    """Silence hmmlearn's per-iteration convergence chatter during a fit.

    hmmlearn reports "Model is not converging" through the *logging* module,
    not ``warnings``, so ``catch_warnings`` does not touch it. Left alone, a
    32-refit backtest prints hundreds of these — and they are almost always
    benign: EM oscillating in the 1e-5 range at the end of a run that has, for
    every practical purpose, converged.

    Suppressing the noise does not suppress the fact. Whether the fit actually
    converged is recorded in :attr:`FitReport.converged` and logged once per
    fit, which is the level at which it is worth knowing.
    """
    logger = logging.getLogger("hmmlearn")
    previous = logger.level
    logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        logger.setLevel(previous)


class NotFittedError(RuntimeError):
    """Raised when inference is attempted before :meth:`RegimeHMM.fit`."""


@dataclass
class FitReport:
    """What happened during Baum-Welch training."""

    log_likelihood: float
    n_iter: int
    converged: bool
    n_restarts: int
    #: Best log-likelihood from each restart, for judging whether the EM surface
    #: is flat (restarts agree) or treacherous (they do not).
    restart_scores: list[float] = field(default_factory=list)
    n_samples: int = 0
    n_features: int = 0

    @property
    def restart_spread(self) -> float:
        """Range of restart log-likelihoods, in nats.

        A large spread means Baum-Welch is finding materially different local
        optima and the resulting regime map should be treated as unstable.
        """
        if len(self.restart_scores) < 2:
            return 0.0
        return float(max(self.restart_scores) - min(self.restart_scores))


class RegimeHMM:
    """A Gaussian-emission HMM over market observation vectors.

    Parameters
    ----------
    cfg:
        :class:`~stockagent.config.HMMConfig` controlling state count,
        covariance structure and restart policy.

    Examples
    --------
    >>> model = RegimeHMM(HMMConfig(n_states=3, n_restarts=2))   # doctest: +SKIP
    >>> model.fit(X_train)                                       # doctest: +SKIP
    >>> probs = model.filter(X_all)      # causal, safe for signals
    >>> path = model.viterbi(X_all)      # most likely state sequence
    """

    def __init__(self, cfg: HMMConfig | None = None) -> None:
        self.cfg = cfg or HMMConfig()
        self.model: GaussianHMM | None = None
        self.fit_report: FitReport | None = None
        self._feature_names: list[str] = []

    # ------------------------------------------------------------------ train

    def fit(self, X: np.ndarray, feature_names: list[str] | None = None) -> "RegimeHMM":
        """Train by Baum-Welch (A.5) with multiple random restarts.

        EM converges to a *local* optimum that depends on initialisation, so a
        single fit is not reproducible science. We run ``n_restarts`` fits from
        different seeds and keep the highest-likelihood model.
        """
        X = self._check_array(X)
        n_samples, n_features = X.shape
        if n_samples < self.cfg.n_states * 20:
            raise ValueError(
                f"{n_samples} samples is too few to estimate {self.cfg.n_states} states "
                f"(need >= {self.cfg.n_states * 20}). Use fewer states or more history."
            )
        self._feature_names = list(feature_names or [f"f{i}" for i in range(n_features)])

        best: GaussianHMM | None = None
        best_score = -np.inf
        scores: list[float] = []

        for restart in range(self.cfg.n_restarts):
            seed = self.cfg.random_state + restart * 997
            candidate = GaussianHMM(
                n_components=self.cfg.n_states,
                covariance_type=self.cfg.covariance_type,
                n_iter=self.cfg.n_iter,
                tol=self.cfg.tol,
                min_covar=self.cfg.min_covar,
                random_state=seed,
                init_params="stmc",
                params="stmc",
            )
            with warnings.catch_warnings(), _quiet_hmmlearn():
                # hmmlearn signals non-convergence two ways -- a warning and a
                # log record. Both are silenced here and recorded in the
                # FitReport instead, so one line per fit replaces hundreds.
                warnings.simplefilter("ignore")
                try:
                    candidate.fit(X)
                    score = float(candidate.score(X))
                except (ValueError, np.linalg.LinAlgError) as exc:
                    LOG.debug("restart %d (seed %d) failed: %s", restart, seed, exc)
                    continue

            if not np.isfinite(score):
                LOG.debug("restart %d produced non-finite score", restart)
                continue
            scores.append(score)
            if score > best_score:
                best_score, best = score, candidate

        if best is None:
            raise RuntimeError(
                f"all {self.cfg.n_restarts} Baum-Welch restarts failed. The feature "
                "matrix may be degenerate (constant or collinear columns)."
            )

        self.model = self._sort_states(best)
        monitor = self.model.monitor_
        self.fit_report = FitReport(
            log_likelihood=best_score,
            n_iter=int(getattr(monitor, "iter", 0)),
            converged=bool(getattr(monitor, "converged", False)),
            n_restarts=len(scores),
            restart_scores=sorted(scores, reverse=True),
            n_samples=n_samples,
            n_features=n_features,
        )
        LOG.info(
            "Baum-Welch: states=%d ll=%.2f iters=%d converged=%s restarts=%d spread=%.2f",
            self.cfg.n_states, best_score, self.fit_report.n_iter,
            self.fit_report.converged, len(scores), self.fit_report.restart_spread,
        )
        if not self.fit_report.converged:
            LOG.warning("Baum-Welch hit the iteration cap (%d) without converging",
                        self.cfg.n_iter)
        return self

    def _sort_states(self, model: GaussianHMM) -> GaussianHMM:
        """Reorder states by mean of the first feature (the return channel).

        HMM state indices are arbitrary labels. Sorting them makes successive
        refits comparable and keeps saved artifacts diffable; semantic naming
        happens separately in :mod:`stockagent.regime.labeling`.
        """
        order = np.argsort(model.means_[:, 0])
        model.means_ = model.means_[order]
        model.startprob_ = model.startprob_[order]
        model.transmat_ = model.transmat_[np.ix_(order, order)]
        # Assigning through `covars_` is not supported; set the private storage
        # that hmmlearn reads, in whichever layout this covariance_type uses.
        covars = model.covars_[order]
        if model.covariance_type == "full":
            model._covars_ = covars
        elif model.covariance_type == "diag":
            model._covars_ = np.array([np.diag(c) for c in covars])
        elif model.covariance_type == "spherical":
            model._covars_ = np.array([np.diag(c).mean() for c in covars])
        elif model.covariance_type == "tied":
            pass  # a single shared matrix; ordering does not apply
        return model

    # -------------------------------------------------------------- emissions

    def emission_logprob(self, X: np.ndarray) -> np.ndarray:
        """log B: ``(T, K)`` matrix of ``log P(o_t | q_t = k)``.

        Computed from the public ``means_``/``covars_`` attributes rather than
        hmmlearn's private ``_compute_log_likelihood``, so an upstream refactor
        cannot silently change our inference.
        """
        self._require_fitted()
        X = self._check_array(X)
        means, covars = self.model.means_, self.model.covars_
        out = np.empty((X.shape[0], self.n_states), dtype=float)
        for k in range(self.n_states):
            # No diagonal jitter here: hmmlearn has already applied min_covar
            # during fitting, and adding more would make our log-likelihood
            # disagree with model.score(). `allow_singular` covers the
            # degenerate case without perturbing well-conditioned matrices.
            out[:, k] = multivariate_normal.logpdf(
                X, mean=means[k], cov=covars[k], allow_singular=True
            )
        return np.nan_to_num(out, nan=_LOG_ZERO, neginf=_LOG_ZERO)

    # ---------------------------------------------------------------- forward

    def forward(self, X: np.ndarray) -> tuple[np.ndarray, float]:
        """Forward algorithm (A.3), in log space.

        Returns ``(log_alpha, log_likelihood)`` where
        ``log_alpha[t, j] = log P(o_1..o_t, q_t = j | lambda)``.
        """
        self._require_fitted()
        log_B = self.emission_logprob(X)
        log_A = self._safe_log(self.model.transmat_)
        log_pi = self._safe_log(self.model.startprob_)

        n_obs = log_B.shape[0]
        log_alpha = np.empty((n_obs, self.n_states), dtype=float)
        log_alpha[0] = log_pi + log_B[0]
        for t in range(1, n_obs):
            # alpha_t(j) = [ sum_i alpha_{t-1}(i) a_ij ] * b_j(o_t)
            log_alpha[t] = logsumexp(log_alpha[t - 1][:, None] + log_A, axis=0) + log_B[t]
        return log_alpha, float(logsumexp(log_alpha[-1]))

    def filter(self, X: np.ndarray) -> np.ndarray:
        """**Causal** state posteriors ``P(q_t | o_1..o_t)``, shape ``(T, K)``.

        This is the only state-probability method that is safe to turn into a
        trading signal: the value at row *t* depends on no observation after
        *t*. Rows sum to 1.
        """
        log_alpha, _ = self.forward(X)
        return np.exp(log_alpha - logsumexp(log_alpha, axis=1, keepdims=True))

    def backward(self, X: np.ndarray) -> np.ndarray:
        """Backward algorithm (A.5): ``log P(o_{t+1}..o_T | q_t = i)``."""
        self._require_fitted()
        log_B = self.emission_logprob(X)
        log_A = self._safe_log(self.model.transmat_)

        n_obs = log_B.shape[0]
        log_beta = np.zeros((n_obs, self.n_states), dtype=float)
        for t in range(n_obs - 2, -1, -1):
            log_beta[t] = logsumexp(log_A + log_B[t + 1] + log_beta[t + 1], axis=1)
        return log_beta

    def smooth(self, X: np.ndarray) -> np.ndarray:
        """Smoothed posteriors ``P(q_t | o_1..o_T)`` -- **uses future data**.

        Correct for labelling historical regimes and for research plots. Never
        use it to generate a signal you intend to trade; see :meth:`filter`.
        """
        log_alpha, _ = self.forward(X)
        log_gamma = log_alpha + self.backward(X)
        return np.exp(log_gamma - logsumexp(log_gamma, axis=1, keepdims=True))

    # ---------------------------------------------------------------- viterbi

    def viterbi(self, X: np.ndarray) -> np.ndarray:
        """Viterbi decoding (A.4): the single most likely state sequence.

        Identical to the forward recursion except it takes the ``max`` over
        previous paths instead of the sum, and keeps backpointers.

        Note that Viterbi is a *whole-sequence* estimate: the state it assigns
        to bar *t* can change when later bars arrive. For live signals prefer
        :meth:`filter`; use Viterbi to describe history.
        """
        self._require_fitted()
        log_B = self.emission_logprob(X)
        log_A = self._safe_log(self.model.transmat_)
        log_pi = self._safe_log(self.model.startprob_)

        n_obs = log_B.shape[0]
        delta = np.empty((n_obs, self.n_states), dtype=float)
        psi = np.zeros((n_obs, self.n_states), dtype=int)

        delta[0] = log_pi + log_B[0]
        for t in range(1, n_obs):
            scores = delta[t - 1][:, None] + log_A   # (i -> j)
            psi[t] = np.argmax(scores, axis=0)
            delta[t] = scores[psi[t], np.arange(self.n_states)] + log_B[t]

        path = np.empty(n_obs, dtype=int)
        path[-1] = int(np.argmax(delta[-1]))
        for t in range(n_obs - 2, -1, -1):
            path[t] = psi[t + 1, path[t + 1]]
        return path

    def log_likelihood(self, X: np.ndarray) -> float:
        """Total log P(O | lambda) via the forward algorithm."""
        return self.forward(X)[1]

    # ------------------------------------------------------------- properties

    @property
    def n_states(self) -> int:
        return self.cfg.n_states

    @property
    def transition_matrix(self) -> np.ndarray:
        self._require_fitted()
        return self.model.transmat_.copy()

    @property
    def means(self) -> np.ndarray:
        self._require_fitted()
        return self.model.means_.copy()

    def expected_duration(self) -> np.ndarray:
        """Expected persistence of each state, in bars: ``1 / (1 - a_ii)``.

        A regime with an expected duration of 2 days is noise, not a regime.
        """
        self._require_fitted()
        stay = np.clip(np.diag(self.model.transmat_), 0.0, 1.0 - 1e-12)
        return 1.0 / (1.0 - stay)

    def stationary_distribution(self) -> np.ndarray:
        """Long-run share of time spent in each state.

        The normalised left eigenvector of A for eigenvalue 1; falls back to a
        uniform distribution if A is defective.
        """
        self._require_fitted()
        values, vectors = np.linalg.eig(self.model.transmat_.T)
        idx = int(np.argmin(np.abs(values - 1.0)))
        vec = np.real(vectors[:, idx])
        total = vec.sum()
        if not np.isfinite(total) or abs(total) < 1e-12:
            return np.full(self.n_states, 1.0 / self.n_states)
        return np.abs(vec / total)

    # ------------------------------------------------------------ persistence

    def save(self, path: pathlib.Path | str) -> pathlib.Path:
        """Pickle the fitted model plus its fit report and feature names."""
        self._require_fitted()
        path = pathlib.Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as handle:
            pickle.dump(
                {
                    "version": 1,
                    "cfg": self.cfg,
                    "model": self.model,
                    "fit_report": self.fit_report,
                    "feature_names": self._feature_names,
                },
                handle,
            )
        LOG.info("saved model -> %s", path)
        return path

    @classmethod
    def load(cls, path: pathlib.Path | str) -> "RegimeHMM":
        """Load a model written by :meth:`save`."""
        with pathlib.Path(path).open("rb") as handle:
            blob: dict[str, Any] = pickle.load(handle)
        obj = cls(blob["cfg"])
        obj.model = blob["model"]
        obj.fit_report = blob.get("fit_report")
        obj._feature_names = blob.get("feature_names", [])
        return obj

    # --------------------------------------------------------------- internal

    def _require_fitted(self) -> None:
        if self.model is None:
            raise NotFittedError("call fit() before running inference")

    @staticmethod
    def _safe_log(arr: np.ndarray) -> np.ndarray:
        """log() that maps exact zeros to a large negative number, not -inf."""
        with np.errstate(divide="ignore"):
            out = np.log(np.asarray(arr, dtype=float))
        return np.where(np.isfinite(out), out, _LOG_ZERO)

    @staticmethod
    def _check_array(X: np.ndarray) -> np.ndarray:
        X = np.asarray(X, dtype=float)
        if X.ndim == 1:
            X = X.reshape(-1, 1)
        if X.ndim != 2:
            raise ValueError(f"expected a 2-D (T, n_features) array, got shape {X.shape}")
        if not np.isfinite(X).all():
            n_bad = int((~np.isfinite(X)).sum())
            raise ValueError(
                f"feature matrix contains {n_bad} NaN/inf values. Drop or impute them "
                "before fitting -- Gaussian emissions cannot absorb them."
            )
        return X
