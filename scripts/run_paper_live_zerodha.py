"""Run paper trading with real Zerodha live market data.

IMPORTANT: This script connects to real Zerodha market data feeds (WebSocket).
It does NOT place real orders. No order placement, modification, or cancellation
is performed. All fills are simulated by PaperExecutionBroker.

Safety requirements:
  - Requires --i-understand-this-uses-live-market-data flag.
  - Refuses to run if LIVE_TRADING_ENABLED=true.
  - Requires ZERODHA_API_KEY and ZERODHA_ACCESS_TOKEN.
  - Does not write to .env or read credentials from code.

Usage:
  python3 scripts/run_paper_live_zerodha.py \\
    --i-understand-this-uses-live-market-data \\
    --symbols RELIANCE INFY \\
    --interval-seconds 60 \\
    --strategy orb \\
    --dashboard-path data/dashboard/session_status.json
"""

from __future__ import annotations

import argparse
import signal
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# ------------------------------------------------------------------
# Imports that do NOT touch Zerodha SDK at import time
# ------------------------------------------------------------------
from trading_engine.backtest.cost_model import CostModel  # noqa: E402
from trading_engine.backtest.slippage_model import SlippageModel  # noqa: E402
from trading_engine.common.config import load_settings  # noqa: E402
from trading_engine.dashboard.session_writer import DashboardSessionWriter  # noqa: E402
from trading_engine.live_data.candle_builder import CandleBuilder  # noqa: E402
from trading_engine.live_data.zerodha_feed import ZerodhaLiveMarketFeed  # noqa: E402
from trading_engine.notifications.telegram import TelegramNotifier  # noqa: E402
from trading_engine.paper.broker import PaperExecutionBroker  # noqa: E402
from trading_engine.paper.live_runner import PaperLiveRunner, PaperLiveRunnerConfig  # noqa: E402
from trading_engine.paper.portfolio import PaperPortfolio  # noqa: E402

_ZERO = Decimal("0")
_SAFETY_FLAG = "--i-understand-this-uses-live-market-data"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run paper trading with real Zerodha live market data.\n"
            "No real orders are placed.\n\n"
            f"Requires: {_SAFETY_FLAG}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        _SAFETY_FLAG,
        dest="safety_flag",
        action="store_true",
        default=False,
        help="Required acknowledgement that live market data will be used.",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        required=True,
        metavar="SYMBOL",
        help="Symbols to trade, e.g. RELIANCE INFY",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=60,
        metavar="N",
        help="Candle interval in seconds (default: 60)",
    )
    parser.add_argument(
        "--strategy",
        choices=["orb", "gap_continuation"],
        default="orb",
        help="Strategy to run (default: orb)",
    )
    parser.add_argument(
        "--gc-quantity",
        type=int,
        default=50,
        metavar="N",
        help="[gap_continuation] Shares per trade (default: 50)",
    )
    parser.add_argument(
        "--gc-min-gap-bps",
        type=float,
        default=120.0,
        metavar="BPS",
        help="[gap_continuation] Minimum gap size in bps (default: 120)",
    )
    parser.add_argument(
        "--gc-max-gap-bps",
        type=float,
        default=500.0,
        metavar="BPS",
        help="[gap_continuation] Maximum gap size in bps (default: 500)",
    )
    parser.add_argument(
        "--gc-trigger-bps",
        type=float,
        default=40.0,
        metavar="BPS",
        help="[gap_continuation] Continuation trigger in bps (default: 40)",
    )
    parser.add_argument(
        "--gc-stop-bps",
        type=float,
        default=120.0,
        metavar="BPS",
        help="[gap_continuation] Stop-loss in bps (default: 120)",
    )
    parser.add_argument(
        "--gc-long-only",
        action="store_true",
        default=False,
        help="[gap_continuation] Only take gap-up LONG continuations",
    )
    parser.add_argument(
        "--gc-short-only",
        action="store_true",
        default=False,
        help="[gap_continuation] Only take gap-down SHORT continuations",
    )
    parser.add_argument(
        "--dashboard-path",
        default="data/dashboard/session_status.json",
        metavar="PATH",
        help="Path to write dashboard JSON (default: data/dashboard/session_status.json)",
    )
    parser.add_argument(
        "--initial-cash",
        type=float,
        default=100000.0,
        metavar="AMOUNT",
        help="Starting portfolio cash (default: 100000)",
    )
    parser.add_argument(
        "--telegram-token",
        default=None,
        metavar="TOKEN",
        help="Telegram bot token from @BotFather (optional)",
    )
    parser.add_argument(
        "--telegram-chat-id",
        default=None,
        metavar="CHAT_ID",
        help="Your Telegram chat ID (optional)",
    )
    return parser.parse_args(argv)


def _build_strategy(name: str, symbols: list[str], args: argparse.Namespace) -> object:
    """Build the strategy object by name."""
    if name == "orb":
        from trading_engine.strategies.orb import (  # noqa: E402
            OpeningRangeBreakoutStrategy,
            ORBConfig,
        )

        config = ORBConfig(
            strategy_id="orb_paper_live",
            quantity=1,
        )
        return OpeningRangeBreakoutStrategy(config=config)

    if name == "gap_continuation":
        from trading_engine.strategies.gap_continuation import (  # noqa: E402
            GapContinuationConfig,
            GapContinuationStrategy,
        )

        allow_long = not args.gc_short_only
        allow_short = not args.gc_long_only
        config = GapContinuationConfig(
            strategy_id="gc_paper_live",
            quantity=args.gc_quantity,
            min_gap_bps=args.gc_min_gap_bps,
            max_gap_bps=args.gc_max_gap_bps,
            continuation_trigger_bps=args.gc_trigger_bps,
            stop_loss_bps=args.gc_stop_bps,
            allow_long_continuations=allow_long,
            allow_short_continuations=allow_short,
        )
        return GapContinuationStrategy(config=config)

    raise ValueError(f"Unknown strategy: {name!r}")


def _resolve_instrument_tokens(
    symbols: list[str],
    api_key: str,
    access_token: str,
) -> tuple[list[int], dict[int, str]]:
    """Fetch NSE instrument list and resolve symbol → token mapping.

    Returns:
        (instrument_tokens, token_symbol_map)
    """
    try:
        from kiteconnect import KiteConnect  # type: ignore[import]
    except ImportError:
        print("[ERROR] kiteconnect is not installed.\nInstall it with: pip install kiteconnect\n")
        sys.exit(1)

    from trading_engine.broker.zerodha.client import ZerodhaBroker  # noqa: E402

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    broker = ZerodhaBroker(kite_client=kite)
    broker.connect()

    instruments = broker.get_instruments("NSE")
    symbol_to_token: dict[str, int] = {
        inst["tradingsymbol"]: inst["instrument_token"]
        for inst in instruments
        if inst.get("tradingsymbol") and inst.get("instrument_token")
    }

    tokens: list[int] = []
    token_symbol_map: dict[int, str] = {}
    missing: list[str] = []
    for sym in symbols:
        token = symbol_to_token.get(sym.upper())
        if token is None:
            missing.append(sym)
        else:
            tokens.append(token)
            token_symbol_map[token] = sym.upper()

    if missing:
        print(f"[ERROR] Could not resolve instrument tokens for: {missing}")
        print("Check that the symbol names are valid NSE tradingsymbols.")
        sys.exit(1)

    return tokens, token_symbol_map


def main(argv: list[str] | None = None) -> int:
    """Entry point; returns exit code."""
    args = _parse_args(argv)

    # ------------------------------------------------------------------
    # Safety checks
    # ------------------------------------------------------------------
    if not args.safety_flag:
        print(
            f"\n[ERROR] You must pass '{_SAFETY_FLAG}' to acknowledge that\n"
            "this script uses real Zerodha market data.\n\n"
            "Example:\n"
            "  python3 scripts/run_paper_live_zerodha.py \\\n"
            f"    {_SAFETY_FLAG} \\\n"
            "    --symbols RELIANCE INFY\n"
        )
        return 1

    settings = load_settings()

    if settings.live_trading_enabled:
        print(
            "\n[SAFETY] LIVE_TRADING_ENABLED=true detected.\n"
            "This script refuses to run when live trading is enabled.\n"
            "Set LIVE_TRADING_ENABLED=false in your .env file.\n"
        )
        return 1

    api_key = settings.zerodha_api_key.get_secret_value()
    access_token = settings.zerodha_access_token.get_secret_value()

    missing_creds = [
        name
        for name, val in [("ZERODHA_API_KEY", api_key), ("ZERODHA_ACCESS_TOKEN", access_token)]
        if not val or val == "replace_me"
    ]
    if missing_creds:
        print(
            f"\n[ERROR] Missing credentials: {missing_creds}\n"
            "Set these in your .env file before running the paper live feed.\n"
            "Run `python3 scripts/zerodha_login_helper.py` to generate an access token.\n"
        )
        return 1

    # ------------------------------------------------------------------
    # Build components
    # ------------------------------------------------------------------
    symbols = [s.upper() for s in args.symbols]
    initial_cash = Decimal(str(args.initial_cash))

    print(f"\n[Paper Live] Resolving instrument tokens for: {symbols} …")
    instrument_tokens, token_symbol_map = _resolve_instrument_tokens(symbols, api_key, access_token)
    print(f"[Paper Live] Resolved: {token_symbol_map}")

    cost = CostModel(
        brokerage_per_order=_ZERO,
        brokerage_cap=_ZERO,
        stt_rate=_ZERO,
        exchange_txn_rate=_ZERO,
        sebi_rate=_ZERO,
        stamp_duty_rate=_ZERO,
        gst_rate=_ZERO,
    )
    slippage = SlippageModel(bps=_ZERO)
    portfolio = PaperPortfolio(initial_cash=initial_cash)
    broker = PaperExecutionBroker(portfolio=portfolio, cost_model=cost, slippage_model=slippage)

    strategy = _build_strategy(args.strategy, symbols, args)

    dashboard_writer: DashboardSessionWriter | None = None
    if args.dashboard_path:
        dashboard_writer = DashboardSessionWriter(output_path=args.dashboard_path)

    notifier: TelegramNotifier | None = None
    if args.telegram_token and args.telegram_chat_id:
        notifier = TelegramNotifier(
            bot_token=args.telegram_token,
            chat_id=args.telegram_chat_id,
        )
        print(f"[Paper Live] Telegram notifications enabled (chat_id={args.telegram_chat_id})")
    else:
        print("[Paper Live] Telegram notifications disabled (pass --telegram-token and --telegram-chat-id to enable)")

    config = PaperLiveRunnerConfig(
        strategy_id=f"{args.strategy}_paper_live",
        symbols=symbols,
        interval_seconds=args.interval_seconds,
        initial_cash=initial_cash,
        dashboard_path=args.dashboard_path,
    )
    candle_builder = CandleBuilder(interval_seconds=args.interval_seconds)
    runner = PaperLiveRunner(
        config=config,
        candle_builder=candle_builder,
        strategy=strategy,
        execution_broker=broker,
        portfolio=portfolio,
        dashboard_writer=dashboard_writer,
        notifier=notifier,
    )

    def _kite_ticker_factory(api_key_: str, access_token_: str) -> object:
        try:
            from kiteconnect import KiteTicker  # type: ignore[import]
        except ImportError:
            print("[ERROR] kiteconnect is not installed. Install with: pip install kiteconnect")
            sys.exit(1)
        ticker = KiteTicker(api_key_, access_token_)
        return ticker

    feed = ZerodhaLiveMarketFeed(
        kite_ticker_factory=_kite_ticker_factory,
        api_key=api_key,
        access_token=access_token,
        instrument_tokens=instrument_tokens,
        token_symbol_map=token_symbol_map,
    )
    feed.set_tick_callback(runner.on_tick)

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------
    runner.start()

    def _handle_signal(signum: int, frame: object) -> None:  # noqa: ARG001
        print("\n[Paper Live] Stopping …")
        runner.stop()
        feed.disconnect()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    gc_info = ""
    if args.strategy == "gap_continuation":
        direction = "long+short"
        if args.gc_long_only:
            direction = "long-only"
        elif args.gc_short_only:
            direction = "short-only"
        gc_info = (
            f"  GC params: min_gap={args.gc_min_gap_bps}bps  max_gap={args.gc_max_gap_bps}bps  "
            f"trigger={args.gc_trigger_bps}bps  stop={args.gc_stop_bps}bps  "
            f"qty={args.gc_quantity}  direction={direction}\n"
        )
    print(
        f"\n[Paper Live] Starting paper trading\n"
        f"  Strategy : {args.strategy}\n"
        f"  Symbols  : {symbols}\n"
        f"  Interval : {args.interval_seconds}s candles\n"
        f"  Cash     : {initial_cash}\n"
        f"{gc_info}"
        "\nPress Ctrl+C to stop.\n"
    )
    feed.connect()

    # Block the main thread until the stop event or KeyboardInterrupt.
    try:
        signal.pause()
    except (KeyboardInterrupt, AttributeError):
        # signal.pause() not available on Windows; KeyboardInterrupt as fallback.
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
