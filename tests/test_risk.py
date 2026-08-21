"""Tests for sizing, exposure limits and configuration validation.

These are the rules that bound the downside, so they are tested against the
arithmetic in ``strategy/risk-management.md`` rather than against themselves.
"""

from __future__ import annotations

import pytest

from stockagent.config import Config, ConfigError, RiskConfig
from stockagent.portfolio import Portfolio


@pytest.fixture
def portfolio() -> Portfolio:
    """Whole-share account. Fractional sizing has its own tests below."""
    return Portfolio(10_000.0, RiskConfig(allow_fractional_shares=False))


def test_fractional_is_the_default() -> None:
    """Most brokers support it and small accounts are unusable without it."""
    assert RiskConfig().allow_fractional_shares is True


def test_worked_example_from_the_docs(portfolio: Portfolio) -> None:
    """The SPY example in strategy/risk-management.md must reproduce exactly."""
    result = portfolio.size(price=773.26, atr=8.94)
    assert result.stop == pytest.approx(755.38, abs=0.01)
    assert result.shares == 5
    assert result.risk_amount == pytest.approx(89.40, abs=0.05)
    assert result.risk_pct_equity == pytest.approx(0.00894, abs=1e-4)
    assert result.targets[0] == pytest.approx(800.08, abs=0.02)
    assert result.targets[1] == pytest.approx(826.90, abs=0.02)


def test_risk_never_exceeds_one_percent(portfolio: Portfolio) -> None:
    for price, atr in [(10.0, 0.4), (50.0, 1.2), (773.0, 9.0), (5.0, 0.05)]:
        result = portfolio.size(price=price, atr=atr)
        if result.is_tradeable:
            assert result.risk_pct_equity <= portfolio.cfg.max_risk_per_trade + 1e-9, (
                f"price={price} atr={atr} risked {result.risk_pct_equity:.4%}")


def test_wider_stop_buys_fewer_shares(portfolio: Portfolio) -> None:
    """The core property of fixed-fractional sizing."""
    tight = portfolio.size(price=100.0, atr=1.0)
    wide = portfolio.size(price=100.0, atr=5.0)
    assert tight.shares > wide.shares
    assert tight.risk_amount == pytest.approx(wide.risk_amount, rel=0.25)


def test_zero_atr_falls_back_to_percentage_stop(portfolio: Portfolio) -> None:
    result = portfolio.size(price=100.0, atr=0.0)
    assert result.stop == pytest.approx(95.0)
    assert result.is_tradeable


def test_position_limit_blocks_the_eleventh() -> None:
    """With heat deliberately slackened, the position count is what binds."""
    pf = Portfolio(1_000_000.0, RiskConfig(max_portfolio_heat=0.90,
                                           max_open_positions=10))
    for i in range(10):
        sized = pf.size(price=10.0, atr=1.0)
        assert sized.is_tradeable, f"position {i} should have been allowed"
        pf.open(f"SYM{i}", sized)

    assert len(pf.positions) == 10
    assert pf.slots_free == 0
    result = pf.size(price=10.0, atr=1.0)
    assert not result.is_tradeable
    assert "max 10" in result.rejected_reason


def test_heat_cap_binds_before_the_position_count(portfolio: Portfolio) -> None:
    """At 1% risk and a 6% cap, the 7th position is refused on heat, not count."""
    opened = 0
    for i in range(portfolio.cfg.max_open_positions):
        sized = portfolio.size(price=10.0, atr=1.0)
        if not sized.is_tradeable:
            break
        portfolio.open(f"SYM{i}", sized)
        opened += 1

    assert opened < portfolio.cfg.max_open_positions, (
        "heat should have bound before the position limit")
    assert portfolio.heat == pytest.approx(portfolio.cfg.max_portfolio_heat, abs=1e-3)
    assert not portfolio.size(price=10.0, atr=1.0).is_tradeable


def test_heat_cap_shrinks_later_positions() -> None:
    pf = Portfolio(100_000.0, RiskConfig(max_risk_per_trade=0.02,
                                         max_portfolio_heat=0.05))
    opened = 0
    for i in range(10):
        sized = pf.size(price=50.0, atr=2.0)
        if not sized.is_tradeable:
            break
        pf.open(f"S{i}", sized)
        opened += 1
        assert pf.heat <= pf.cfg.max_portfolio_heat + 1e-9
    assert opened >= 2
    assert pf.heat <= 0.05 + 1e-9


def test_cannot_exceed_cash(portfolio: Portfolio) -> None:
    """No leverage: sizing is capped by cash even when risk allows more."""
    result = portfolio.size(price=9_000.0, atr=1.0)
    assert result.shares <= 1


def test_sizing_refuses_rather_than_raising() -> None:
    """A refusal must carry a reason the Risk Agent can report."""
    pf = Portfolio(50.0, RiskConfig(allow_fractional_shares=False))
    result = pf.size(price=773.0, atr=9.0)
    assert not result.is_tradeable
    assert result.rejected_reason


def test_fractional_rescues_what_whole_shares_refuse() -> None:
    """The same $50 account, same stock: refused whole, sized fractionally."""
    price, atr = 773.0, 9.0
    assert not Portfolio(50.0, RiskConfig(allow_fractional_shares=False)).size(
        price=price, atr=atr).is_tradeable

    result = Portfolio(50.0, RiskConfig(allow_fractional_shares=True)).size(
        price=price, atr=atr)
    assert result.is_tradeable
    assert result.risk_pct_equity == pytest.approx(0.01, abs=1e-4)


def test_r_multiple_on_close() -> None:
    pf = Portfolio(10_000.0, RiskConfig())
    sized = pf.size(price=100.0, atr=1.0)
    pf.open("TEST", sized)
    risk_per_share = 100.0 - sized.stop
    record = pf.close("TEST", 100.0 + 2.0 * risk_per_share, reason="target")
    assert record["r_multiple"] == pytest.approx(2.0, rel=1e-6)


def test_stop_loss_gives_negative_one_r() -> None:
    pf = Portfolio(10_000.0, RiskConfig())
    sized = pf.size(price=100.0, atr=1.0)
    pf.open("TEST", sized)
    record = pf.close("TEST", sized.stop, reason="stopped")
    assert record["r_multiple"] == pytest.approx(-1.0, rel=1e-6)


# ------------------------------------------------- small account, fractional


def _small(**kwargs) -> Portfolio:
    """The real account: $217.73, cash, no positions.

    Defaults to whole shares so the tests below can contrast the two modes;
    pass ``allow_fractional_shares=True`` for the production default.
    """
    kwargs.setdefault("allow_fractional_shares", False)
    return Portfolio(217.73, RiskConfig(**kwargs))


def test_small_account_refuses_expensive_whole_shares() -> None:
    """At $218 equity the 1% budget is $2.18; one SPY share risks ~$17.75."""
    result = _small().size(price=773.26, atr=8.87)
    assert not result.is_tradeable
    assert "risk budget" in result.rejected_reason


def test_small_account_can_take_a_cheap_whole_share() -> None:
    """ERIC at $10.15 with a $0.29 ATR fits inside the same $2.18 budget."""
    result = _small().size(price=10.15, atr=0.29)
    assert result.is_tradeable
    assert result.shares == 3
    assert result.risk_pct_equity <= 0.01 + 1e-9


def test_fractional_sizing_unlocks_any_price() -> None:
    result = _small(allow_fractional_shares=True).size(price=773.26, atr=8.87)
    assert result.is_tradeable
    assert 0 < result.shares < 1
    assert result.fractional
    # The whole point: risk lands on the cap instead of rounding to nothing.
    assert result.risk_pct_equity == pytest.approx(0.01, abs=1e-4)


def test_fractional_never_rounds_risk_above_the_cap() -> None:
    """Rounding down is not cosmetic -- rounding up breaches the cap every trade."""
    pf = _small(allow_fractional_shares=True)
    for price, atr in [(773.26, 8.87), (10.15, 0.29), (64_867.0, 1443.9), (0.97, 0.031)]:
        result = pf.size(price=price, atr=atr)
        if result.is_tradeable:
            assert result.risk_pct_equity <= pf.cfg.max_risk_per_trade + 1e-9, (
                f"price={price} risked {result.risk_pct_equity:.6%}")


def test_fractional_quantity_respects_precision() -> None:
    result = _small(allow_fractional_shares=True, fractional_precision=3).size(
        price=773.26, atr=8.87)
    assert result.shares == pytest.approx(round(result.shares, 3))


def test_min_position_value_blocks_dust() -> None:
    """A sub-dollar position is not a trade; the spread eats it.

    Position value under fractional sizing is ``budget x price / risk_per_share``,
    so dust needs a *high ATR relative to price* -- a volatile penny name, not
    merely an expensive one. At $50 equity, a $4.00 stock with a $1.20 ATR
    sizes to $0.83 of stock.
    """
    pf = Portfolio(50.0, RiskConfig(allow_fractional_shares=True,
                                    min_position_value=5.0))
    result = pf.size(price=4.00, atr=1.20)
    assert not result.is_tradeable
    assert "minimum" in result.rejected_reason


def test_feasibility_explains_a_refusal() -> None:
    report = _small().feasibility(price=773.26, atr=8.87)
    assert report["tradeable"] is False
    assert report["risk_budget"] == pytest.approx(2.18, abs=0.01)  # rounded for display
    assert report["reason"]
    assert report["affordable"] is False   # one share costs more than the account


def test_feasibility_reports_a_workable_trade() -> None:
    report = _small().feasibility(price=10.15, atr=0.29)
    assert report["tradeable"] is True
    assert report["shares"] == 3
    assert report["affordable"] is True


# ------------------------------------------------------------------ config


def test_rejects_reckless_risk_per_trade() -> None:
    cfg = Config()
    cfg.risk.max_risk_per_trade = 0.25
    with pytest.raises(ConfigError, match="survivable"):
        cfg.validate()


def test_rejects_zero_execution_lag() -> None:
    """Executing on the bar that generated the signal is lookahead bias."""
    cfg = Config()
    cfg.backtest.execution_lag = 0
    with pytest.raises(ConfigError, match="lookahead"):
        cfg.validate()


def test_rejects_heat_below_per_trade_risk() -> None:
    cfg = Config()
    cfg.risk.max_portfolio_heat = 0.005
    with pytest.raises(ConfigError, match="heat"):
        cfg.validate()


def test_rejects_out_of_range_state_count() -> None:
    cfg = Config()
    cfg.hmm.n_states = 99
    with pytest.raises(ConfigError, match="n_states"):
        cfg.validate()


def test_default_config_is_valid() -> None:
    Config().validate()
