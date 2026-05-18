"""Live order pilot CLI — places a REAL order via Zerodha.

THIS SCRIPT PLACES REAL ORDERS.  It requires:
  - --i-understand-this-places-real-orders flag
  - Interactive confirmation: type "PLACE LIVE ORDER" exactly
  - LIVE_ORDER_EXECUTION_ENABLED=true
  - LIVE_ORDER_PILOT_ENABLED=true
  - LIVE_TRADING_ENABLED=true
  - Valid Zerodha credentials (API key + access token)
  - Symbol in LIVE_ALLOWED_SYMBOLS

Usage::

    python scripts/live_order_pilot.py \\
        --symbol RELIANCE \\
        --side BUY \\
        --quantity 1 \\
        --order-type MARKET \\
        --i-understand-this-places-real-orders

DO NOT run this in a CI/CD pipeline or automated test.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

# Ensure project src is importable when script is run directly.
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from trading_engine.common.config import load_settings  # noqa: E402
from trading_engine.live_execution.approvals import LiveOrderApprovalGate  # noqa: E402
from trading_engine.live_execution.audit import ApprovalAuditLogger  # noqa: E402
from trading_engine.live_execution.models import ApprovalMode  # noqa: E402
from trading_engine.live_execution.pilot_config import LivePilotConfig  # noqa: E402
from trading_engine.live_execution.pilot_executor import LiveOrderPilotExecutor  # noqa: E402
from trading_engine.live_execution.safety import LiveExecutionSafetyGuard  # noqa: E402
from trading_engine.strategy.signals import OrderIntent  # noqa: E402

_CONFIRMATION_PHRASE = "PLACE LIVE ORDER"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Live order pilot — places a real order via Zerodha.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "WARNING: This script places REAL orders with real money.\n"
            "Use live_order_dry_run.py for simulation."
        ),
    )
    parser.add_argument("--symbol", required=True, help="Trading symbol, e.g. RELIANCE")
    parser.add_argument(
        "--side", required=True, choices=["BUY", "SELL"], help="Order side"
    )
    parser.add_argument("--quantity", required=True, type=int, help="Number of shares (>0)")
    parser.add_argument(
        "--order-type",
        required=True,
        choices=["MARKET", "LIMIT", "SL", "SL-M"],
        dest="order_type",
        help="Order type",
    )
    parser.add_argument("--price", default=None, help="Limit/SL price (required for LIMIT/SL)")
    parser.add_argument("--trigger-price", default=None, dest="trigger_price")
    parser.add_argument("--product", default="MIS", choices=["MIS", "CNC", "NRML"])
    parser.add_argument("--strategy-id", default="pilot", dest="strategy_id")
    parser.add_argument("--exchange", default="NSE")
    parser.add_argument(
        "--i-understand-this-places-real-orders",
        action="store_true",
        dest="confirmed_flag",
        help="Required safety acknowledgement flag.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip interactive confirmation (for testing only; still requires --i-understand-this-places-real-orders).",
    )
    return parser.parse_args(argv)


def _build_intent(args: argparse.Namespace) -> OrderIntent:
    if args.quantity <= 0:
        print(
            json.dumps({"error": "quantity must be positive", "exit_code": 1}),
            file=sys.stderr,
        )
        sys.exit(1)

    price = None
    if args.price is not None:
        try:
            price = Decimal(args.price)
            if price <= 0:
                raise ValueError
        except (InvalidOperation, ValueError):
            print(
                json.dumps({"error": f"invalid price: {args.price!r}", "exit_code": 1}),
                file=sys.stderr,
            )
            sys.exit(1)

    if args.order_type in ("LIMIT", "SL") and price is None:
        print(
            json.dumps({"error": f"--price is required for {args.order_type} orders", "exit_code": 1}),
            file=sys.stderr,
        )
        sys.exit(1)

    trigger_price = None
    if args.trigger_price is not None:
        try:
            trigger_price = Decimal(args.trigger_price)
        except InvalidOperation:
            print(
                json.dumps(
                    {"error": f"invalid trigger_price: {args.trigger_price!r}", "exit_code": 1}
                ),
                file=sys.stderr,
            )
            sys.exit(1)

    return OrderIntent(
        strategy_id=args.strategy_id,
        symbol=args.symbol.upper(),
        exchange=args.exchange.upper(),
        side=args.side,
        quantity=args.quantity,
        order_type=args.order_type,
        product=args.product,
        price=price,
        trigger_price=trigger_price,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # Safety flag check
    if not args.confirmed_flag:
        print(
            json.dumps(
                {
                    "error": "Missing required safety flag: --i-understand-this-places-real-orders",
                    "hint": "Add this flag to acknowledge that this script places real orders.",
                }
            ),
            file=sys.stderr,
        )
        return 2

    settings = load_settings()

    # Hard block: refuse if pilot flags are off
    if not getattr(settings, "live_order_execution_enabled", False):
        print(
            json.dumps(
                {
                    "error": "LIVE_ORDER_EXECUTION_ENABLED is not set to true.",
                    "hint": "Set LIVE_ORDER_EXECUTION_ENABLED=true in your .env to proceed.",
                }
            ),
            file=sys.stderr,
        )
        return 3

    if not getattr(settings, "live_order_pilot_enabled", False):
        print(
            json.dumps(
                {
                    "error": "LIVE_ORDER_PILOT_ENABLED is not set to true.",
                    "hint": "Set LIVE_ORDER_PILOT_ENABLED=true in your .env to proceed.",
                }
            ),
            file=sys.stderr,
        )
        return 3

    intent = _build_intent(args)

    # Interactive confirmation (unless --yes bypasses it)
    if not args.yes:
        print(
            f"\n  WARNING: This will place a REAL order:\n"
            f"    Symbol:     {intent.symbol}\n"
            f"    Side:       {intent.side}\n"
            f"    Quantity:   {intent.quantity}\n"
            f"    Order type: {intent.order_type}\n"
            f"    Product:    {intent.product}\n"
            f"    Exchange:   {intent.exchange}\n"
        )
        try:
            answer = input(f'  Type "{_CONFIRMATION_PHRASE}" to confirm: ').strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.", file=sys.stderr)
            return 4
        if answer != _CONFIRMATION_PHRASE:
            print(
                json.dumps({"error": "Confirmation phrase did not match. Order not placed."}),
                file=sys.stderr,
            )
            return 4

    # Build dependencies
    pilot_config = LivePilotConfig.from_settings(settings)
    safety_guard = LiveExecutionSafetyGuard(settings=settings)
    approval_gate = LiveOrderApprovalGate(mode=ApprovalMode.AUTO_PAPER)
    audit_logger = ApprovalAuditLogger(log_path="data/audit/pilot_orders.jsonl")

    # Connect broker (requires valid credentials in settings)
    try:
        from kiteconnect import KiteConnect

        kite = KiteConnect(api_key=settings.zerodha_api_key.get_secret_value())
        kite.set_access_token(settings.zerodha_access_token.get_secret_value())
    except ImportError:
        print(
            json.dumps({"error": "kiteconnect package not installed. Run: pip install kiteconnect"}),
            file=sys.stderr,
        )
        return 5

    from trading_engine.broker.zerodha.client import ZerodhaBroker

    broker = ZerodhaBroker(kite_client=kite, settings=settings)
    broker.connect()

    executor = LiveOrderPilotExecutor(
        broker=broker,
        pilot_config=pilot_config,
        approval_gate=approval_gate,
        safety_guard=safety_guard,
        audit_logger=audit_logger,
    )

    result = executor.execute(order_intent=intent)

    output = result.to_dict()
    output["symbol"] = intent.symbol
    output["side"] = str(intent.side)
    output["quantity"] = intent.quantity
    output["order_type"] = str(intent.order_type)

    print(json.dumps(output, indent=2))
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
