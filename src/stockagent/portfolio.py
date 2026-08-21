"""Position sizing, stop placement and exposure accounting.

The sizing rule is fixed-fractional risk, which inverts the usual question.
Instead of "how many shares do I want", it asks "how far away is the stop, and
how many shares make that distance cost exactly 1% of equity":

    risk_budget   = equity * max_risk_per_trade
    risk_per_share = |entry - stop|
    shares         = floor(risk_budget / risk_per_share)

The consequence is that a wide stop buys fewer shares, so every position risks
the same amount regardless of how volatile the instrument is. Stops are placed
at a multiple of ATR rather than a round percentage, so the distance adapts to
what the instrument is actually doing.

Two limits sit above the per-trade rule: a cap on the number of open positions,
and a cap on *portfolio heat* -- the sum of open risk across all positions. Heat
is the one that matters. Ten positions each risking 1% is a 10% drawdown if they
are correlated and all stop out together, which in a real correction is exactly
what they do.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from .config import RiskConfig
from .logging_setup import get_logger

LOG = get_logger("portfolio")


@dataclass
class Position:
    """One open position."""

    symbol: str
    #: Float, because fractional orders are supported. Whole-share accounts
    #: simply carry an integral value here.
    shares: float
    entry: float
    stop: float
    targets: list[float] = field(default_factory=list)
    opened: pd.Timestamp | None = None
    direction: str = "long"

    @property
    def risk_per_share(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def open_risk(self) -> float:
        """Currency lost if the stop fills at exactly the stop price."""
        return self.risk_per_share * self.shares

    @property
    def cost_basis(self) -> float:
        return self.entry * self.shares

    def market_value(self, price: float) -> float:
        return price * self.shares

    def unrealized(self, price: float) -> float:
        sign = 1.0 if self.direction == "long" else -1.0
        return sign * (price - self.entry) * self.shares

    def distance_to_stop(self, price: float) -> float:
        """Fractional distance from ``price`` to the stop. Negative once through."""
        if price <= 0:
            return 0.0
        return (price - self.stop) / price if self.direction == "long" else (self.stop - price) / price

    def is_stopped(self, price: float) -> bool:
        return price <= self.stop if self.direction == "long" else price >= self.stop


@dataclass
class SizingResult:
    """The outcome of a sizing request, including the reason for a refusal."""

    shares: float
    entry: float
    stop: float
    targets: list[float]
    risk_amount: float
    risk_pct_equity: float
    #: Populated when ``shares == 0``; explains why the trade cannot be taken.
    rejected_reason: str = ""
    #: True when the quantity is not a whole number.
    fractional: bool = False

    @property
    def is_tradeable(self) -> bool:
        return self.shares > 0 and not self.rejected_reason

    @property
    def position_value(self) -> float:
        return self.shares * self.entry

    def quantity_str(self) -> str:
        return f"{self.shares:g}" if self.fractional else f"{int(self.shares)}"


class Portfolio:
    """Account state plus the sizing and exposure rules that constrain it."""

    def __init__(self, equity: float, cfg: RiskConfig | None = None,
                 *, cash: float | None = None) -> None:
        if equity <= 0:
            raise ValueError(f"equity must be positive, got {equity}")
        self.cfg = cfg or RiskConfig()
        self.starting_equity = float(equity)
        self.equity = float(equity)
        self.cash = float(equity if cash is None else cash)
        self.positions: dict[str, Position] = {}
        self.closed: list[dict[str, Any]] = []

    # ------------------------------------------------------------- accounting

    @property
    def open_risk(self) -> float:
        """Total currency at risk across open positions."""
        return sum(p.open_risk for p in self.positions.values())

    @property
    def heat(self) -> float:
        """Open risk as a fraction of equity."""
        return self.open_risk / self.equity if self.equity > 0 else 0.0

    @property
    def remaining_heat(self) -> float:
        """Risk budget still available before the heat cap binds."""
        return max(0.0, self.cfg.max_portfolio_heat - self.heat)

    @property
    def slots_free(self) -> int:
        return max(0, self.cfg.max_open_positions - len(self.positions))

    def market_value(self, prices: dict[str, float]) -> float:
        return sum(p.market_value(prices.get(sym, p.entry))
                   for sym, p in self.positions.items())

    def total_value(self, prices: dict[str, float]) -> float:
        return self.cash + self.market_value(prices)

    # ---------------------------------------------------------------- sizing

    def stop_for(self, price: float, atr: float, *, direction: str = "long",
                 multiple: float | None = None) -> float:
        """ATR-based stop. Falls back to a 5% stop if ATR is unavailable."""
        multiple = self.cfg.atr_stop_multiple if multiple is None else multiple
        if not atr or not math.isfinite(atr) or atr <= 0:
            LOG.warning("no usable ATR at price %.4f; falling back to a 5%% stop", price)
            distance = price * 0.05
        else:
            distance = atr * multiple
        return price - distance if direction == "long" else price + distance

    def targets_for(self, entry: float, stop: float, *,
                    direction: str = "long") -> list[float]:
        """Take-profit levels at the configured R multiples."""
        risk = abs(entry - stop)
        sign = 1.0 if direction == "long" else -1.0
        return [round(entry + sign * risk * r, 4) for r in self.cfg.target_r_multiples]

    def size(self, price: float, atr: float, *, direction: str = "long",
             stop: float | None = None, equity: float | None = None) -> SizingResult:
        """Size a position under every standing limit.

        Returns a :class:`SizingResult` with ``shares == 0`` and a reason rather
        than raising, so the Risk Agent can report *why* a trade was refused.
        """
        equity = self.equity if equity is None else equity
        stop = self.stop_for(price, atr, direction=direction) if stop is None else stop
        targets = self.targets_for(price, stop, direction=direction)
        risk_per_share = abs(price - stop)

        fractional = self.cfg.allow_fractional_shares

        def refuse(reason: str) -> SizingResult:
            return SizingResult(0.0, price, stop, targets, 0.0, 0.0, reason, fractional)

        if risk_per_share <= 0:
            return refuse("stop coincides with entry; risk per share is zero")
        if self.slots_free <= 0:
            return refuse(
                f"already holding {len(self.positions)} positions "
                f"(max {self.cfg.max_open_positions})"
            )

        # The binding budget is the smaller of the per-trade cap and whatever
        # heat is left. Without the second term, ten 1% trades quietly become a
        # 10% portfolio risk.
        per_trade_budget = equity * self.cfg.max_risk_per_trade
        budget = min(per_trade_budget, self.remaining_heat * equity)
        if budget <= 0:
            return refuse(
                f"portfolio heat {self.heat:.2%} is at the "
                f"{self.cfg.max_portfolio_heat:.2%} cap; no risk budget left"
            )

        raw_shares = budget / risk_per_share
        if fractional:
            # Round *down* to the broker's precision. Rounding up would breach
            # the risk cap by a hair on every trade, and the whole point of the
            # cap is that it is never breached.
            factor = 10**self.cfg.fractional_precision
            shares = math.floor(raw_shares * factor) / factor
        else:
            shares = float(math.floor(raw_shares))

        if shares <= 0:
            unit = "the minimum fraction" if fractional else "a single share"
            hint = "" if fractional else (
                f" -- at this equity a whole-share position needs risk/share "
                f"below {budget:.2f}, i.e. a price under roughly "
                f"{budget / (2 * self.cfg.atr_stop_multiple * 0.0125):,.0f} for a "
                "typically-volatile stock. Enable fractional shares to size any price."
            )
            return refuse(
                f"risk budget {budget:.2f} is smaller than the {risk_per_share:.2f} "
                f"risked by {unit}{hint}"
            )

        # Cash constraint: no leverage.
        if price <= 0:
            return refuse("non-positive price")
        affordable = self.cash / price
        if not fractional:
            affordable = float(math.floor(affordable))
        if affordable <= 0:
            return refuse(f"cash {self.cash:.2f} cannot buy one share at {price:.2f}")
        if shares > affordable:
            LOG.debug("sizing limited by cash: %g -> %g shares", shares, affordable)
            shares = affordable

        value = shares * price
        if value < self.cfg.min_position_value:
            return refuse(
                f"position would be worth {value:.2f}, below the "
                f"{self.cfg.min_position_value:.2f} minimum; the spread and the "
                "attention cost more than it can return"
            )

        risk_amount = shares * risk_per_share
        return SizingResult(
            shares=shares, entry=price, stop=stop, targets=targets,
            risk_amount=risk_amount,
            risk_pct_equity=risk_amount / equity if equity > 0 else 0.0,
            fractional=fractional and shares != int(shares),
        )

    # ------------------------------------------------------------ transitions

    def open(self, symbol: str, result: SizingResult, *,
             when: pd.Timestamp | None = None, direction: str = "long") -> Position:
        """Record a new position. Raises if it would breach a standing limit."""
        if not result.is_tradeable:
            raise ValueError(f"{symbol}: not tradeable ({result.rejected_reason})")
        if symbol in self.positions:
            raise ValueError(f"{symbol}: already held; scale-ins are not modelled")
        if self.slots_free <= 0:
            raise ValueError(f"{symbol}: position limit {self.cfg.max_open_positions} reached")

        cost = result.shares * result.entry
        if cost > self.cash + 1e-9:
            raise ValueError(f"{symbol}: cost {cost:.2f} exceeds cash {self.cash:.2f}")

        position = Position(symbol=symbol, shares=result.shares, entry=result.entry,
                            stop=result.stop, targets=list(result.targets),
                            opened=when, direction=direction)
        self.positions[symbol] = position
        self.cash -= cost
        LOG.info("open %s x%s @ %.4f stop %.4f (risk %.2f = %.2f%% of equity)",
                 symbol, result.quantity_str(), result.entry, result.stop,
                 result.risk_amount, result.risk_pct_equity * 100)
        return position

    def close(self, symbol: str, price: float, *, when: pd.Timestamp | None = None,
              reason: str = "") -> dict[str, Any]:
        """Close a position and record the result in R multiples."""
        if symbol not in self.positions:
            raise KeyError(f"{symbol}: no such open position")
        position = self.positions.pop(symbol)
        pnl = position.unrealized(price)
        self.cash += position.market_value(price)
        self.equity = self.cash + self.market_value({})

        risk = position.open_risk
        record = {
            "symbol": symbol, "shares": position.shares, "entry": position.entry,
            "exit": price, "stop": position.stop, "pnl": pnl,
            # R-multiple is the only P/L unit that compares trades of different
            # sizes fairly: it asks "how many times my planned risk did I make".
            "r_multiple": pnl / risk if risk > 0 else 0.0,
            "opened": position.opened, "closed": when, "reason": reason,
        }
        self.closed.append(record)
        LOG.info("close %s @ %.4f: pnl %.2f (%.2fR) [%s]",
                 symbol, price, pnl, record["r_multiple"], reason or "unspecified")
        return record

    def mark_to_market(self, prices: dict[str, float]) -> float:
        """Refresh equity from current prices and return it."""
        self.equity = self.total_value(prices)
        return self.equity

    def feasibility(self, price: float, atr: float) -> dict[str, Any]:
        """Can this account take a position in this instrument at all?

        Separated from :meth:`size` because "no" has several different causes
        and they call for different responses. Running out of risk budget means
        wait; being unable to afford one share means this instrument is out of
        reach at this account size, permanently, until either the account grows
        or fractional orders are enabled.
        """
        sized = self.size(price, atr)
        risk_per_share = abs(price - sized.stop)
        budget = self.equity * self.cfg.max_risk_per_trade

        return {
            "price": round(price, 4),
            "atr": round(atr, 4) if atr and math.isfinite(atr) else None,
            "risk_per_share": round(risk_per_share, 4),
            "risk_budget": round(budget, 2),
            "shares": sized.shares,
            "position_value": round(sized.position_value, 2),
            "risk_amount": round(sized.risk_amount, 2),
            "risk_pct_equity": round(sized.risk_pct_equity, 5),
            "tradeable": sized.is_tradeable,
            "reason": sized.rejected_reason,
            "affordable": price <= self.cash,
            #: Whole-share sizing needs risk/share <= the budget. Since
            #: risk/share is atr_stop_multiple x ATR, this is the price at
            #: which one share first fits the risk cap.
            "max_price_whole_shares": (
                round(budget / (risk_per_share / price), 2)
                if price > 0 and risk_per_share > 0 else None
            ),
        }

    def snapshot(self, prices: dict[str, float] | None = None) -> dict[str, Any]:
        """Account state in the shape :class:`AnalysisContext` expects."""
        prices = prices or {}
        return {
            "equity": round(self.equity, 2),
            "cash": round(self.cash, 2),
            "open_positions": len(self.positions),
            "max_positions": self.cfg.max_open_positions,
            "slots_free": self.slots_free,
            "heat": round(self.heat, 5),
            "max_heat": self.cfg.max_portfolio_heat,
            "remaining_heat": round(self.remaining_heat, 5),
            "closed_trades": len(self.closed),
            "positions": {
                sym: {
                    "shares": p.shares, "entry": p.entry, "stop": p.stop,
                    "open_risk": round(p.open_risk, 2),
                    "distance_to_stop": round(p.distance_to_stop(prices.get(sym, p.entry)), 4),
                    "unrealized": round(p.unrealized(prices.get(sym, p.entry)), 2),
                }
                for sym, p in self.positions.items()
            },
        }
