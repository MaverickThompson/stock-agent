"""Command-line interface.

    python -m stockagent regime   SPY
    python -m stockagent analyze  SPY --equity 10000
    python -m stockagent backtest SPY
    python -m stockagent scan
    python -m stockagent states   SPY --save artifacts/spy_hmm.pkl

Every command writes a narrative log to ``logs/stockagent.log`` and, where a
decision is made, an audit record to ``logs/decisions.jsonl``.
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys

import pandas as pd

from .backtest import regime_conditional_returns, run_backtest
from .config import Config, ConfigError
from .data_io import DataError, load_prices, load_universe
from .logging_setup import DecisionLog, configure_logging
from .live import summarize as summarize_live
from .pipeline import RegimeEngine, analyze_symbol, live_context
from .portfolio import Portfolio

DISCLAIMER = (
    "Analysis only. Not financial advice, and no order is placed by this program. "
    "Probabilistic output; no outcome is guaranteed."
)


def _cfg(args: argparse.Namespace) -> Config:
    cfg = Config.load(args.config) if args.config else Config()
    if getattr(args, "states", None):
        cfg.hmm.n_states = args.states
    if getattr(args, "restarts", None):
        cfg.hmm.n_restarts = args.restarts
    cfg.validate()
    return cfg


def cmd_regime(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    prices = load_prices(cfg.data.data_dir / f"{args.symbol}.csv", symbol=args.symbol)
    engine = RegimeEngine(cfg).fit(prices)
    state = engine.state_at(-1)

    print(f"\n{args.symbol} regime as of {prices.index[-1].date()}")
    print("=" * 62)
    print(f"  Regime      : {state.label}")
    print(f"  Confidence  : {state.confidence:.1%}  (filtered posterior, no lookahead)")
    print(f"  Exposure    : {state.exposure:.1%}  (volatility-targeted baseline)")
    print(f"  Persistence : {state.diagnostics['expected_duration']:.0f} bars expected")
    print(f"  P(leave)    : {state.diagnostics['transition_out_prob']:.1%} per day")
    print("\n  Posterior by regime:")
    for label, prob in sorted(state.probs.items(), key=lambda kv: -kv[1]):
        bar = "#" * int(round(prob * 40))
        print(f"    {label:<16} {prob:>6.1%} {bar}")

    print("\n  Fitted states:")
    print("    " + engine.regime_map.to_frame().round(3).to_string().replace("\n", "\n    "))

    warnings = state.diagnostics.get("model_warnings") or []
    if warnings:
        print("\n  Model warnings:")
        for warning in warnings:
            print(f"    ! {warning}")

    if args.history:
        print(f"\n  Last {args.history} bars:")
        history = engine.history().tail(args.history)
        print("    " + history[["filtered_label", "filtered_confidence",
                                "viterbi_label", "exposure"]]
              .round(3).to_string().replace("\n", "\n    "))
    print(f"\n{DISCLAIMER}\n")
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    decision_log = DecisionLog(cfg.log_dir / "decisions.jsonl")
    live = live_context(cfg)
    account = live.get("account") or {}

    # Default to the real balance when the connector supplied one. Sizing
    # against a made-up 10,000 while holding 217 produces position sizes you
    # cannot take, which is worse than refusing to size at all.
    if args.equity is not None:
        equity, source = float(args.equity), "--equity"
    elif account.get("equity"):
        equity, source = float(account["equity"]), f"live account {account.get('account_number', '')}".strip()
    else:
        equity, source = 10_000.0, "default (no account data)"

    if args.fractional:
        cfg.risk.allow_fractional_shares = True

    cash = float(account.get("cash", equity)) if account.get("equity") and args.equity is None else equity
    portfolio = Portfolio(equity, cfg.risk, cash=cash)

    print(f"\nSizing against {equity:,.2f} equity from {source}"
          + ("  [fractional]" if cfg.risk.allow_fractional_shares else ""))
    print("Live overlay:")
    for line in summarize_live(live):
        print(f"  {line}")

    result, _engine = analyze_symbol(
        args.symbol, cfg, portfolio=portfolio,
        decision_log=decision_log, live=live,
    )
    print()
    print(result.explain())
    print(f"\n  Audit trail: {decision_log.path} (run {decision_log.run_id})")
    print(f"\n{DISCLAIMER}\n")

    if args.json:
        pathlib.Path(args.json).write_text(
            json.dumps(result.to_dict(), indent=2, default=str), encoding="utf-8")
        print(f"  Wrote {args.json}")
    return 0 if result.approved else 2


def cmd_backtest(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    if args.trials:
        cfg.backtest.n_trials = args.trials
    prices = load_prices(cfg.data.data_dir / f"{args.symbol}.csv", symbol=args.symbol)
    result = run_backtest(prices, cfg, symbol=args.symbol)

    if cfg.backtest.n_trials <= 1:
        print("\n  NOTE: --trials defaults to 1, meaning no correction for multiple\n"
              "  testing. If you tried several state counts, feature sets or\n"
              "  thresholds before keeping this one, pass the real number — the\n"
              "  Sharpe below is otherwise flattered by the search.")

    print(f"\nWalk-forward backtest: {args.symbol}")
    print("=" * 62)
    print(result.summary())
    print("\nBenchmark returns conditional on the signalled regime:")
    conditional = regime_conditional_returns(result)
    if conditional.empty:
        print("  (no regimes recorded)")
    else:
        print("  " + conditional.round(4).to_string().replace("\n", "\n  "))

    excess = result.stats["excess_cagr"]
    print()
    if excess > 0:
        print(f"  Strategy beat buy-and-hold by {excess:.2%}/yr before tax.")
    else:
        print(f"  Strategy LOST to buy-and-hold by {abs(excess):.2%}/yr. On this "
              "evidence the signal does not justify trading it.")
    print("  Costs are modelled at "
          f"{cfg.backtest.cost_bps:.0f}bps per unit of turnover; taxes are not modelled.")

    if args.csv:
        frame = pd.DataFrame({
            "equity": result.equity, "benchmark": result.benchmark,
            "exposure": result.exposure, "regime": result.regimes,
        })
        frame.to_csv(args.csv)
        print(f"  Wrote {args.csv}")
    print(f"\n{DISCLAIMER}\n")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    from .agents.discovery_agent import DiscoveryAgent
    from .indicators import add_all_indicators

    cfg = _cfg(args)
    universe = load_universe(cfg.data.data_dir, cfg.data.universe or None,
                             min_bars=cfg.data.min_bars)
    scored = {sym: add_all_indicators(df) for sym, df in universe.items()}
    ranked = DiscoveryAgent(scored, min_dollar_volume=cfg.risk.min_dollar_volume,
                            top_n=args.top).rank()

    print(f"\nWatchlist ({len(ranked)} of {len(universe)} passed the liquidity gate)")
    print("=" * 62)
    for i, candidate in enumerate(ranked[:args.top], 1):
        print(f"{i:>3}. {candidate.symbol:<8} {candidate.score:>+7.3f}  "
              f"{candidate.price:>10,.2f}")
        print(f"      {', '.join(f'{k} {v:+.2f}' for k, v in candidate.components.items())}")
        for note in candidate.notes:
            print(f"      - {note}")
    print(f"\n{DISCLAIMER}\n")
    return 0


def cmd_account(args: argparse.Namespace) -> int:
    """Report real account state and what it can actually trade."""
    from .indicators import add_all_indicators

    cfg = _cfg(args)
    live = live_context(cfg)
    account = live.get("account") or {}

    equity = float(args.equity if args.equity else account.get("equity", 0.0) or 0.0)
    if equity <= 0:
        print("\nNo account balance available.", file=sys.stderr)
        print("Either pass --equity, or refresh data/live_snapshot.json with an\n"
              "'account' block from your brokerage connector.\n", file=sys.stderr)
        return 1

    cash = float(account.get("cash", equity))
    if args.fractional:
        cfg.risk.allow_fractional_shares = True

    print(f"\nAccount {account.get('account_number', '(from --equity)')}"
          f"  [{account.get('broker', 'unspecified broker')}]")
    print("=" * 74)
    print(f"  Equity          : {equity:>12,.2f}")
    print(f"  Cash            : {cash:>12,.2f}"
          + (f"  ({account.get('settled_cash')} settled)"
             if account.get("settled_cash") is not None else ""))
    print(f"  Open positions  : {len(account.get('positions') or {}):>12d}")
    print(f"  Risk per trade  : {equity * cfg.risk.max_risk_per_trade:>12,.2f}"
          f"   ({cfg.risk.max_risk_per_trade:.1%} of equity)")
    print(f"  Portfolio heat  : {equity * cfg.risk.max_portfolio_heat:>12,.2f}"
          f"   ({cfg.risk.max_portfolio_heat:.1%} cap)")
    print(f"  Fractional      : {'ENABLED' if cfg.risk.allow_fractional_shares else 'off':>12}")
    if account.get("asof"):
        print(f"  As of           : {account['asof']:>12}")

    universe = load_universe(cfg.data.data_dir, cfg.data.universe or None,
                             min_bars=cfg.data.min_bars)
    portfolio = Portfolio(equity, cfg.risk, cash=cash)

    rows, skipped = [], []
    for symbol, frame in sorted(universe.items()):
        # An index carries no volume and cannot be bought. Listing ^VIX as a
        # position you could take would be flatly wrong, so it is excluded
        # here rather than being shown with a misleading share count.
        if (frame["volume"] <= 0).all():
            skipped.append(symbol)
            continue
        enriched = add_all_indicators(frame)
        last = enriched.iloc[-1]
        price, atr_value = float(last["close"]), float(last["atr"])
        if not (price > 0):
            continue
        rows.append((symbol, portfolio.feasibility(price, atr_value)))

    print(f"\n  What this account can take a position in "
          f"({'fractional' if cfg.risk.allow_fractional_shares else 'whole shares'}):")
    print(f"    {'symbol':<8} {'price':>10} {'risk/sh':>9} {'qty':>12} "
          f"{'value':>10} {'risk':>8} {'%eq':>7}  status")
    for symbol, f in rows:
        if f["tradeable"]:
            status = "OK"
            qty = f"{f['shares']:g}"
        else:
            status = f["reason"].split(" -- ")[0][:44]
            qty = "-"
        print(f"    {symbol:<8} {f['price']:>10,.2f} {f['risk_per_share']:>9,.2f} "
              f"{qty:>12} {f['position_value']:>10,.2f} {f['risk_amount']:>8,.2f} "
              f"{f['risk_pct_equity']:>6.2%}  {status}")

    tradeable = [s for s, f in rows if f["tradeable"]]
    budget = equity * cfg.risk.max_risk_per_trade
    print(f"\n  {len(tradeable)} of {len(rows)} tradeable: "
          f"{', '.join(tradeable) if tradeable else 'none'}")
    if skipped:
        print(f"  excluded (index, not purchasable): {', '.join(skipped)}")

    if not cfg.risk.allow_fractional_shares:
        # Typical liquid equity runs ~1.25% daily ATR; at 2x that is 2.5% of
        # price per share of risk. Solving budget = 0.025 * price gives the
        # price ceiling at which one whole share still fits the 1% cap.
        ceiling = budget / (cfg.risk.atr_stop_multiple * 0.0125)
        print(f"\n  At {equity:,.2f} equity your risk budget is {budget:,.2f} per trade.")
        print(f"  With whole shares that caps you at roughly {ceiling:,.0f} per share")
        print(f"  for a typically-volatile stock. Re-run with --fractional to size")
        print(f"  any price precisely; Webull supports fractional equity orders.")
    print(f"\n{DISCLAIMER}\n")
    return 0


def cmd_states(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    prices = load_prices(cfg.data.data_dir / f"{args.symbol}.csv", symbol=args.symbol)
    engine = RegimeEngine(cfg).fit(prices)

    print(f"\n{args.symbol}: {cfg.hmm.n_states}-state HMM, "
          f"{len(engine.features)} observations")
    print("=" * 62)
    print(engine.regime_map.to_frame().round(4).to_string())
    print("\nTransition matrix (row -> column):")
    labels = [lab.value[:8] for lab in engine.regime_map.labels()]
    print(pd.DataFrame(engine.model.transition_matrix,
                       index=labels, columns=labels).round(4).to_string())
    print("\nStationary distribution:")
    for label, share in zip(labels, engine.model.stationary_distribution()):
        print(f"  {label:<16} {share:>6.1%}")
    report = engine.model.fit_report
    print(f"\nBaum-Welch: ll={report.log_likelihood:.2f} iters={report.n_iter} "
          f"converged={report.converged} restarts={report.n_restarts} "
          f"spread={report.restart_spread:.1f} nats")

    if args.save:
        engine.model.save(args.save)
        print(f"\nSaved model -> {args.save}")
    print(f"\n{DISCLAIMER}\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stockagent",
        description="HMM market-regime detection with a five-agent review process.",
        epilog=DISCLAIMER,
    )
    parser.add_argument("--config", help="path to a JSON config overlay")
    parser.add_argument("--states", type=int, help="number of hidden states (2-6)")
    parser.add_argument("--restarts", type=int, help="Baum-Welch random restarts")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("regime", help="current regime for a symbol")
    p.add_argument("symbol")
    p.add_argument("--history", type=int, default=0, metavar="N",
                   help="also print the last N bars")
    p.set_defaults(func=cmd_regime)

    p = sub.add_parser("analyze", help="run the five-agent debate")
    p.add_argument("symbol")
    p.add_argument("--equity", type=float, default=None,
                   help="account equity for sizing (default: the live account balance)")
    p.add_argument("--fractional", action="store_true",
                   help="size with fractional shares")
    p.add_argument("--json", help="write the full debate record to this path")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("backtest", help="walk-forward evaluation")
    p.add_argument("symbol")
    p.add_argument("--csv", help="write the equity curve to this path")
    p.add_argument("--trials", type=int, default=None, metavar="N",
                   help="how many strategy variations you tried (deflates the "
                        "Sharpe for multiple testing). Be honest.")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("scan", help="rank the universe")
    p.add_argument("--top", type=int, default=10)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("account", help="real balance and what it can trade")
    p.add_argument("--equity", type=float, default=None,
                   help="override the balance from the live snapshot")
    p.add_argument("--fractional", action="store_true",
                   help="size with fractional shares")
    p.set_defaults(func=cmd_account)

    p = sub.add_parser("states", help="inspect the fitted HMM")
    p.add_argument("symbol")
    p.add_argument("--save", help="pickle the fitted model to this path")
    p.set_defaults(func=cmd_states)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = _cfg(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1

    configure_logging(cfg.log_dir, level=logging.DEBUG if args.verbose else logging.INFO)
    try:
        return int(args.func(args))
    except (DataError, ConfigError) as exc:
        print(f"\nerror: {exc}\n", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"\nerror: {exc}\nRun:  python scripts/fetch_data.py\n", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
