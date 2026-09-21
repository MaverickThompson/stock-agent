"""One market session of the 60-day study, start to finish.

Order of operations, which is not arbitrary
-------------------------------------------
1. **Manage open positions first.** Section 5 exits -- stop, Target 1, Target 2,
   falsification, 45-day time stop -- are evaluated before any new entry is
   considered. Capital and the Section 6 position count must be freed before
   they are spent, or the caps are applied against stale state.
2. **Then consider new entries**, in rank order, re-checking the Section 6
   limits after each fill rather than once at the start.

Everything is logged. Section 10: "EVERY signal is logged, including rejected
ones and errors. Logging only executed trades produces survivorship bias and is
indistinguishable from cherry-picking." A candidate that fails a gate produces a
REJECTED row with its reason; a candidate the session never reaches produces a
SKIPPED row; a failure anywhere produces SYSTEM_ERROR.

The analysis layer is injected rather than imported directly, so the session can
be exercised without the HMM, the five-agent debate or a live broker. The real
adapters live in :func:`default_candidate_source` and
:func:`default_thesis_source`.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Any, Callable, Protocol, Sequence

from . import study_rules as rules
from .study_log import StudyLog, utc_now_iso

try:
    from .logging_setup import get_logger
except ImportError:  # pragma: no cover
    import logging

    def get_logger(name: str):
        return logging.getLogger(name)

LOG = get_logger("session")


@dataclasses.dataclass(frozen=True)
class Candidate:
    """A symbol that passed Layer 1 and is up for the Layer 2 debate."""
    symbol: str
    score: float
    sector: str = "unknown"


@dataclasses.dataclass(frozen=True)
class Thesis:
    """Written BEFORE entry. Section 5 requires every field here to exist first."""
    entry_zone: tuple[float, float]
    stop: float
    target_1: float
    target_2: float
    predicted_probability: float
    falsification: str
    layer_2_favoured: bool
    rationale: str
    hours_to_earnings: float | None = None

    def as_text(self) -> str:
        return (f"entry {self.entry_zone[0]:.2f}-{self.entry_zone[1]:.2f}, "
                f"stop {self.stop:.2f}, T1 {self.target_1:.2f}, T2 {self.target_2:.2f}, "
                f"p={self.predicted_probability:.2f}; {self.rationale}")


@dataclasses.dataclass
class OpenPosition:
    ticker: str
    direction: str
    entry_price: float
    size: int
    remaining: int
    stop: float
    target_1: float
    target_2: float
    entry_timestamp: str
    thesis: str
    invalidation: str
    target_1_hit: bool = False

    def age_days(self, now: dt.datetime) -> float:
        entered = dt.datetime.strptime(self.entry_timestamp, "%Y-%m-%dT%H:%M:%SZ")
        return (now - entered.replace(tzinfo=dt.timezone.utc)).total_seconds() / 86400.0


class BrokerLike(Protocol):
    def account(self) -> Any: ...
    def positions(self) -> dict[str, int]: ...
    def market_is_open(self) -> bool: ...
    def quote(self, symbol: str) -> Any: ...
    def submit(self, symbol: str, qty: int, side: str, *, quote: Any = None) -> Any: ...


def evaluate_exit(position: OpenPosition, bid: float, now: dt.datetime,
                  falsified: bool = False) -> tuple[str, int] | None:
    """Section 5 exits, in the protocol's own precedence order.

    Returns ``(reason, quantity)`` or None. Stop is checked first: when a bar
    touches both the stop and a target, assuming the favourable one is the
    single most common way a paper study flatters itself.
    """
    if bid <= position.stop:
        return ("stop", position.remaining)
    if falsified:
        return ("falsification", position.remaining)
    if not position.target_1_hit and bid >= position.target_1:
        return ("target_1", max(1, position.size // 2))
    if position.target_1_hit and bid >= position.target_2:
        return ("target_2", position.remaining)
    if position.age_days(now) >= rules.TIME_STOP_DAYS:
        return ("time_stop", position.remaining)
    return None


@dataclasses.dataclass
class SessionResult:
    entered: int = 0
    exited: int = 0
    rejected: int = 0
    errors: int = 0
    skipped: int = 0

    def summary(self) -> str:
        return (f"entered={self.entered} exited={self.exited} "
                f"rejected={self.rejected} skipped={self.skipped} errors={self.errors}")


def run_session(*, broker: BrokerLike, log: StudyLog,
                candidates: Sequence[Candidate],
                thesis_for: Callable[[Candidate], Thesis | None],
                open_positions: list[OpenPosition],
                sector_weights: dict[str, float] | None = None,
                now: dt.datetime | None = None,
                falsified: Callable[[OpenPosition], bool] | None = None,
                ) -> SessionResult:
    """Execute one session. Never raises for a per-symbol failure -- it logs."""
    now = now or dt.datetime.now(dt.timezone.utc)
    sector_weights = dict(sector_weights or {})
    result = SessionResult()

    if not broker.market_is_open():
        log.log_system_error(stage="market_closed",
                             detail="session invoked while the market was closed; "
                                    "no signals evaluated")
        result.errors += 1
        return result

    # -- 1. exits before entries -------------------------------------------
    for position in list(open_positions):
        try:
            quote = broker.quote(position.ticker)
            decision = evaluate_exit(
                position, quote.bid, now,
                falsified=bool(falsified and falsified(position)))
            if decision is None:
                continue
            reason, qty = decision
            qty = min(qty, position.remaining)
            fill = broker.submit(position.ticker, qty, "sell", quote=quote)
            log.close_trade(
                entry_timestamp=position.entry_timestamp, ticker=position.ticker,
                direction=position.direction, entry_price=position.entry_price,
                size=qty, thesis_at_entry=position.thesis,
                invalidation_condition=position.invalidation,
                exit_timestamp=fill.filled_at, exit_price=fill.filled_price,
                exit_reason=reason,
                was_thesis_correct=(reason in ("target_1", "target_2")))
            log.log_signal(ticker=position.ticker, signal_type="exit",
                           triggered_rule=reason, action_taken="EXITED",
                           price_at_signal=fill.filled_price,
                           notes=f"qty {qty} of {position.remaining}; "
                                 f"quote {quote.timestamp}")
            position.remaining -= qty
            if reason == "target_1":
                position.target_1_hit = True
                position.stop = position.entry_price   # Section 5: stop to entry
            if position.remaining <= 0:
                open_positions.remove(position)
            result.exited += 1
        except Exception as exc:  # noqa: BLE001 - logged, never fatal
            LOG.exception("exit handling failed for %s", position.ticker)
            log.log_system_error(stage="exit", detail=f"{type(exc).__name__}: {exc}",
                                 ticker=position.ticker)
            result.errors += 1

    # -- 2. new entries -----------------------------------------------------
    account = broker.account()
    nlv = float(account.equity)
    open_risk = len(open_positions) * rules.RISK_PER_TRADE

    for candidate in candidates:
        if len(open_positions) >= rules.MAX_CONCURRENT_POSITIONS:
            log.log_signal(ticker=candidate.symbol, signal_type="entry_candidate",
                           triggered_rule="layer1_rank", action_taken="SKIPPED",
                           notes="position cap reached earlier in this session")
            result.skipped += 1
            continue
        try:
            thesis = thesis_for(candidate)
            if thesis is None:
                log.log_signal(ticker=candidate.symbol, signal_type="entry_candidate",
                               triggered_rule="layer2_debate", action_taken="REJECTED",
                               reason_if_rejected="layer 2 produced no thesis")
                result.rejected += 1
                continue

            quote = broker.quote(candidate.symbol)
            cash_fraction = float(account.cash) / nlv if nlv else 0.0
            try:
                shares = rules.position_size(nlv, quote.ask, thesis.stop)
            except ValueError:
                # Stop at or above the ask. The gate below rejects it with a
                # reason; sizing must not raise before that happens.
                shares = 0
            prospective_weight = (shares * quote.ask / nlv) if nlv and shares else 0.0

            gate = rules.check_entry(
                entry_ask=quote.ask, stop=thesis.stop,
                target_1_bid=thesis.target_1, target_2_bid=thesis.target_2,
                predicted_probability=thesis.predicted_probability,
                entry_zone=thesis.entry_zone, nlv=nlv,
                open_positions=len(open_positions), open_risk_fraction=open_risk,
                cash_fraction=cash_fraction,
                sector_weight_after=sector_weights.get(candidate.sector, 0.0)
                + prospective_weight,
                hours_to_earnings=thesis.hours_to_earnings,
                layer_1_passed=True, layer_2_favoured=thesis.layer_2_favoured)

            if not gate.passed:
                log.log_signal(ticker=candidate.symbol, signal_type="entry_candidate",
                               triggered_rule="section5_gate", action_taken="REJECTED",
                               price_at_signal=quote.ask,
                               reason_if_rejected=gate.reason,
                               notes=f"quote {quote.timestamp}")
                result.rejected += 1
                continue

            size = rules.position_size(nlv, quote.ask, thesis.stop)
            fill = broker.submit(candidate.symbol, size, "buy", quote=quote)
            thesis_text = thesis.as_text()

            log.open_trade(entry_timestamp=fill.filled_at, ticker=candidate.symbol,
                           direction="long", entry_price=fill.filled_price, size=size,
                           thesis_at_entry=thesis_text,
                           invalidation_condition=thesis.falsification)
            log.log_signal(ticker=candidate.symbol, signal_type="entry_candidate",
                           triggered_rule="section5_gate", action_taken="ENTERED",
                           price_at_signal=fill.filled_price,
                           notes=f"{size} shares; stop {thesis.stop:.2f}; "
                                 f"quote {quote.timestamp}; order {fill.order_id}")

            open_positions.append(OpenPosition(
                ticker=candidate.symbol, direction="long",
                entry_price=fill.filled_price, size=size, remaining=size,
                stop=thesis.stop, target_1=thesis.target_1, target_2=thesis.target_2,
                entry_timestamp=fill.filled_at, thesis=thesis_text,
                invalidation=thesis.falsification))
            sector_weights[candidate.sector] = (
                sector_weights.get(candidate.sector, 0.0) + prospective_weight)
            open_risk += rules.RISK_PER_TRADE
            result.entered += 1

        except Exception as exc:  # noqa: BLE001 - logged, never fatal
            LOG.exception("entry handling failed for %s", candidate.symbol)
            log.log_system_error(stage="entry", detail=f"{type(exc).__name__}: {exc}",
                                 ticker=candidate.symbol)
            result.errors += 1

    LOG.info("session complete: %s", result.summary())
    return result
