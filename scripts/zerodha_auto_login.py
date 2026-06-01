"""Automated Zerodha daily token refresh using Playwright + pyotp.

Automates the full Zerodha login flow:
  1. Opens the Zerodha login page in a headless browser.
  2. Enters your user ID and password.
  3. Generates the TOTP code from your TOTP secret key.
  4. Captures the request_token from the redirect URL.
  5. Exchanges it for an access_token and writes it to .env.

Requirements:
    pip install playwright pyotp
    playwright install chromium

Credentials required in .env (NEVER commit these):
    ZERODHA_USER_ID       — your Zerodha client ID (e.g. AB1234)
    ZERODHA_PASSWORD      — your Zerodha login password
    ZERODHA_TOTP_SECRET   — the TOTP secret key (NOT a 6-digit code;
                            the base32 string from when you set up 2FA)
    ZERODHA_API_KEY       — Kite Connect API key
    ZERODHA_API_SECRET    — Kite Connect API secret

Usage:
    python3 scripts/zerodha_auto_login.py
    python3 scripts/zerodha_auto_login.py --env-file .env
    python3 scripts/zerodha_auto_login.py --headed     # show browser window

Schedule on Windows (run once at 8:55 AM weekdays):
    See bottom of this file for Task Scheduler instructions.

Note: Automated login is technically against Zerodha ToS but is widely used
by retail algo traders. Use at your own discretion.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Automated Zerodha daily token refresh.")
    parser.add_argument("--env-file", default=".env", help="Path to .env file (default: .env)")
    parser.add_argument(
        "--headed",
        action="store_true",
        default=False,
        help="Show the browser window (useful for debugging)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="Page action timeout in seconds (default: 30)",
    )
    return parser.parse_args(argv)


def _load_auto_login_credentials(env_path: Path) -> tuple[str, str, str]:
    """Read ZERODHA_USER_ID, ZERODHA_PASSWORD, ZERODHA_TOTP_SECRET from .env.

    Returns:
        (user_id, password, totp_secret)

    Raises:
        SystemExit if any credential is missing.
    """
    if not env_path.exists():
        print(f"[ERROR] .env file not found: {env_path}")
        sys.exit(1)

    creds: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        creds[key.strip()] = val.strip()

    required = {
        "ZERODHA_USER_ID": "your Zerodha client ID (e.g. AB1234)",
        "ZERODHA_PASSWORD": "your Zerodha login password",
        "ZERODHA_TOTP_SECRET": "base32 TOTP secret key from Zerodha 2FA setup",
    }
    missing = [k for k in required if not creds.get(k) or creds[k] in ("replace_me", "")]
    if missing:
        print("\n[ERROR] Missing credentials in .env:")
        for k in missing:
            print(f"  {k}  —  {required[k]}")
        print("\nAdd these to your .env file and re-run.\n")
        sys.exit(1)

    return creds["ZERODHA_USER_ID"], creds["ZERODHA_PASSWORD"], creds["ZERODHA_TOTP_SECRET"]


def _extract_request_token(url: str) -> str | None:
    """Parse request_token from a redirect URL."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    tokens = params.get("request_token", [])
    return tokens[0] if tokens else None


def run_auto_login(
    env_path: Path,
    headed: bool = False,
    timeout_seconds: int = 30,
) -> int:
    """Run the full automated login flow. Returns 0 on success, 1 on failure."""
    # ------------------------------------------------------------------
    # Import checks
    # ------------------------------------------------------------------
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import]
    except ImportError:
        print(
            "[ERROR] playwright is not installed.\n"
            "Install with:\n"
            "  pip install playwright\n"
            "  playwright install chromium\n"
        )
        return 1

    try:
        import pyotp  # type: ignore[import]
    except ImportError:
        print("[ERROR] pyotp is not installed.\nInstall with: pip install pyotp\n")
        return 1

    try:
        from kiteconnect import KiteConnect  # type: ignore[import]
    except ImportError:
        print("[ERROR] kiteconnect is not installed.\nInstall with: pip install kiteconnect\n")
        return 1

    from trading_engine.broker.zerodha.login import exchange_request_token, update_env_file
    from trading_engine.common.config import load_settings

    settings = load_settings()
    api_key = settings.zerodha_api_key.get_secret_value()
    api_secret = settings.zerodha_api_secret.get_secret_value()

    if not api_key or api_key == "replace_me":
        print("[ERROR] ZERODHA_API_KEY not set in .env")
        return 1
    if not api_secret or api_secret == "replace_me":
        print("[ERROR] ZERODHA_API_SECRET not set in .env")
        return 1

    user_id, password, totp_secret = _load_auto_login_credentials(env_path)

    login_url = f"https://kite.zerodha.com/connect/login?api_key={api_key}&v=3"
    timeout_ms = timeout_seconds * 1000

    print("\n=== Zerodha Auto Login ===")
    print(f"  User ID  : {user_id}")
    print(f"  Env file : {env_path}")
    print(f"  Headed   : {headed}")
    print()

    request_token: str | None = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context()
        page = context.new_page()

        try:
            # Step 1 — load login page
            print("[1/5] Loading Zerodha login page...")
            page.goto(login_url, timeout=timeout_ms)
            page.wait_for_selector("input[type='text']", timeout=timeout_ms)

            # Step 2 — enter user ID and password
            print("[2/5] Entering credentials...")
            page.fill("input[type='text']", user_id)
            page.fill("input[type='password']", password)
            page.click("button[type='submit']")

            # Step 3 — wait for TOTP input
            print("[3/5] Waiting for TOTP page...")
            page.wait_for_selector("input[type='number'], input[placeholder*='TOTP'], input[placeholder*='totp']", timeout=timeout_ms)

            # Step 4 — generate and enter TOTP
            print("[4/5] Generating TOTP code...")
            totp = pyotp.TOTP(totp_secret)
            code = totp.now()
            page.fill("input[type='number'], input[placeholder*='TOTP'], input[placeholder*='totp']", code)
            page.click("button[type='submit']")

            # Step 5 — wait for redirect and capture request_token
            print("[5/5] Waiting for redirect...")
            # Zerodha redirects to the app's redirect URL after login.
            # We intercept the navigation to capture request_token.
            for _ in range(timeout_seconds * 2):
                current_url = page.url
                token = _extract_request_token(current_url)
                if token:
                    request_token = token
                    break
                time.sleep(0.5)

            if not request_token:
                print(f"\n[ERROR] request_token not found in redirect URL.\nFinal URL: {page.url}")
                print("Try running with --headed to see what happened.\n")
                return 1

        except Exception as exc:
            print(f"\n[ERROR] Browser automation failed: {exc}")
            if not headed:
                print("Try running with --headed to debug.\n")
            return 1
        finally:
            browser.close()

    # ------------------------------------------------------------------
    # Exchange request_token → access_token
    # ------------------------------------------------------------------
    print("\nExchanging request_token for access_token...")
    try:
        kite = KiteConnect(api_key=api_key)
        access_token = exchange_request_token(settings, kite, request_token)
    except Exception as exc:
        print(f"[ERROR] Token exchange failed: {exc}")
        return 1

    # ------------------------------------------------------------------
    # Write to .env
    # ------------------------------------------------------------------
    try:
        update_env_file(env_path, access_token)
    except Exception as exc:
        print(f"[ERROR] Failed to write token to {env_path}: {exc}")
        return 1

    print(f"\n[OK] Access token refreshed and written to {env_path}")
    print("     (Token value not logged.)\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    env_path = Path(args.env_file).resolve()
    return run_auto_login(env_path, headed=args.headed, timeout_seconds=args.timeout)


if __name__ == "__main__":
    sys.exit(main())


# =============================================================================
# WINDOWS TASK SCHEDULER SETUP
# =============================================================================
#
# Run this once in PowerShell (as Administrator) to schedule the auto-login
# at 8:55 AM every weekday:
#
#   $action = New-ScheduledTaskAction `
#       -Execute "python" `
#       -Argument "C:\path\to\scripts\zerodha_auto_login.py --env-file C:\path\to\.env" `
#       -WorkingDirectory "C:\path\to\GPT Build Pack"
#
#   $trigger = New-ScheduledTaskTrigger `
#       -Weekly `
#       -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
#       -At 8:55am
#
#   $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
#
#   Register-ScheduledTask `
#       -TaskName "ZerodhaAutoLogin" `
#       -Action $action `
#       -Trigger $trigger `
#       -Settings $settings `
#       -RunLevel Highest
#
# To test it immediately:
#   Start-ScheduledTask -TaskName "ZerodhaAutoLogin"
#
# To remove it:
#   Unregister-ScheduledTask -TaskName "ZerodhaAutoLogin" -Confirm:$false
#
# =============================================================================
