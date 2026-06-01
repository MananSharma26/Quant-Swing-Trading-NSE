"""Reusable Zerodha historical data download logic.

Keeps scripts thin by providing all orchestration here:
  - Safety guard (LIVE_TRADING_ENABLED must be False)
  - Symbol → instrument_token mapping
  - Download orchestration via HistoricalDataDownloader
  - Chunked date-range splitting for intraday intervals (Zerodha ≤60-day limit)
  - Merge with existing Parquet, deduplication, and sorting
  - Dry-run support
  - Clean result reporting

No KiteConnect import at the module level — the Kite client is injected.
No live order placement.  Read-only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
import time

import pandas as pd

from trading_engine.data.historical import HistoricalDataDownloader
from trading_engine.data.universe import UniverseConfig
from trading_engine.data.validation import DataValidationReport, validate_ohlcv_dataframe

logger = logging.getLogger(__name__)

# Zerodha limits intraday (sub-day) intervals to 60-day windows per request.
INTRADAY_INTERVALS = {
    "minute",
    "3minute",
    "5minute",
    "10minute",
    "15minute",
    "30minute",
    "60minute",
}


# ---------------------------------------------------------------------------
# Date-range chunking
# ---------------------------------------------------------------------------


def split_date_range(
    from_date: datetime,
    to_date: datetime,
    chunk_days: int,
) -> list[tuple[datetime, datetime]]:
    """Split a date range into chunks of at most *chunk_days* calendar days.

    Args:
        from_date:  Start of the full date range (inclusive).
        to_date:    End of the full date range (inclusive).
        chunk_days: Maximum size of each chunk in calendar days.

    Returns:
        List of (chunk_from, chunk_to) tuples that together cover the full range.

    Raises:
        ValueError: If chunk_days is not positive.
    """
    if chunk_days <= 0:
        raise ValueError(f"chunk_days must be positive, got {chunk_days}")
    chunks: list[tuple[datetime, datetime]] = []
    current = from_date
    while current <= to_date:
        end = min(current + timedelta(days=chunk_days - 1), to_date)
        chunks.append((current, end))
        current = end + timedelta(days=1)
    return chunks


# ---------------------------------------------------------------------------
# Configuration dataclass
# ---------------------------------------------------------------------------


@dataclass
class DownloadConfig:
    """All parameters for a historical data download run.

    Args:
        universe:    Universe defining the target symbol list and exchange.
        interval:    Candle interval, e.g. "5minute", "minute", "day".
        from_date:   Start of the download date range.
        to_date:     End of the download date range.
        data_dir:    Root directory for Parquet storage.
        symbols:     If non-empty, download only these symbols (overrides universe).
        dry_run:     If True, log what would be done without calling the broker.
        save:        If True, write Parquet files (ignored when dry_run=True).
        chunk_days:  Maximum calendar days per API request for intraday intervals.
        replace:     If True, overwrite any existing Parquet instead of merging.
    """

    universe: UniverseConfig
    interval: str
    from_date: datetime
    to_date: datetime
    data_dir: str | Path = "./data"
    symbols: list[str] = field(default_factory=list)
    dry_run: bool = False
    save: bool = True
    chunk_days: int = 60
    replace: bool = False

    def target_symbols(self) -> list[str]:
        """Return the effective symbol list for this download."""
        return list(self.symbols) if self.symbols else self.universe.get_symbols()


# ---------------------------------------------------------------------------
# Download result
# ---------------------------------------------------------------------------


@dataclass
class DownloadResult:
    """Summary of a completed (or dry-run) download."""

    symbols_requested: list[str]
    symbols_downloaded: list[str]
    symbols_failed: list[str]
    symbols_missing_token: list[str]
    validation_reports: dict[str, DataValidationReport]
    file_paths: dict[str, Path]
    dry_run: bool
    chunks: list[tuple[datetime, datetime]] = field(default_factory=list)

    def print_summary(self) -> None:
        """Print a human-readable summary to stdout."""
        tag = "[DRY RUN] " if self.dry_run else ""
        print(f"\n{tag}Download summary")
        print(f"  Requested:       {self.symbols_requested}")
        print(f"  Downloaded:      {self.symbols_downloaded}")
        print(f"  No token found:  {self.symbols_missing_token}")
        print(f"  Failed:          {self.symbols_failed}")
        if self.chunks:
            print(f"  Chunks:          {len(self.chunks)}")
            for i, (c_from, c_to) in enumerate(self.chunks, 1):
                print(f"    {i}: {c_from.date()} -> {c_to.date()}")
        if self.file_paths:
            print("  Saved files:")
            for sym, path in self.file_paths.items():
                print(f"    {sym}: {path}")
        if self.validation_reports:
            print("  Validation:")
            for sym, rpt in self.validation_reports.items():
                status = "OK" if rpt.is_valid else "INVALID"
                warnings = [i for i in rpt.issues if i.severity == "warning"]
                errors = [i for i in rpt.issues if i.severity == "error"]
                print(
                    f"    {sym}: {status} | rows={rpt.row_count} | "
                    f"errors={len(errors)} | warnings={len(warnings)}"
                )


# ---------------------------------------------------------------------------
# Instrument mapping helper
# ---------------------------------------------------------------------------


def build_instrument_map(
    instruments: list[dict[str, Any]],
    symbols: list[str],
    exchange: str = "NSE",
) -> tuple[dict[str, int], list[str]]:
    """Build a symbol → instrument_token map from a Zerodha instruments list.

    Args:
        instruments: Raw instrument dicts from ZerodhaBroker.get_instruments().
                     Each dict must have "tradingsymbol", "instrument_token",
                     and optionally "exchange".
        symbols:     Symbol names to look up.
        exchange:    Exchange string to filter by (default: "NSE").

    Returns:
        (found_map, missing_symbols) where:
          - found_map: {symbol: instrument_token} for symbols that were found.
          - missing_symbols: list of symbols with no matching instrument.
    """
    # Build a lookup from the full instruments list.
    lookup: dict[str, int] = {}
    for inst in instruments:
        sym = inst.get("tradingsymbol", "")
        exch = inst.get("exchange", "")
        token = inst.get("instrument_token")
        if sym and token is not None and (not exchange or exch == exchange):
            lookup[sym] = int(token)

    found: dict[str, int] = {}
    missing: list[str] = []
    for symbol in symbols:
        if symbol in lookup:
            found[symbol] = lookup[symbol]
        else:
            missing.append(symbol)
            logger.warning("No instrument token found for %s on %s.", symbol, exchange)

    return found, missing


# ---------------------------------------------------------------------------
# Safety guard
# ---------------------------------------------------------------------------


def assert_live_trading_disabled(settings: Any) -> None:
    """Raise SystemExit if LIVE_TRADING_ENABLED is True.

    This is a hard safety gate: the download script must refuse to run when
    live trading is enabled, to avoid any accidental order placement.

    Args:
        settings: Any object with a ``live_trading_enabled`` bool attribute.

    Raises:
        SystemExit: if live_trading_enabled is True.
    """
    if getattr(settings, "live_trading_enabled", False):
        raise SystemExit(
            "\n[SAFETY] LIVE_TRADING_ENABLED=true detected.\n"
            "The historical download script refuses to run when live trading is enabled.\n"
            "Set LIVE_TRADING_ENABLED=false in your .env file before downloading data.\n"
        )


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


def run_download(
    config: DownloadConfig,
    broker: Any,
    *,
    logger: logging.Logger | None = None,
) -> DownloadResult:
    """Download historical candles for the configured symbols.

    For intraday intervals the date range is automatically split into chunks of
    at most ``config.chunk_days`` calendar days so that each API request stays
    within Zerodha's 60-day limit.  After downloading all chunks, any existing
    Parquet file for the same symbol/interval is loaded and merged (unless
    ``config.replace=True``), the result is deduplicated by timestamp, sorted,
    and saved back.

    Args:
        config: DownloadConfig with all parameters.
        broker: Any broker that implements get_instruments() and get_historical_data().
        logger: Optional logger; defaults to module logger.

    Returns:
        DownloadResult with a full summary.
    """
    log = logger or logging.getLogger(__name__)
    target = config.target_symbols()
    exchange_str = str(config.universe.exchange)
    exchange = config.universe.exchange

    # Determine chunk boundaries upfront (used in both dry-run and real download).
    is_intraday = config.interval in INTRADAY_INTERVALS
    if is_intraday:
        chunks = split_date_range(config.from_date, config.to_date, config.chunk_days)
    else:
        chunks = [(config.from_date, config.to_date)]

    if config.dry_run:
        log.info("[DRY RUN] Would download %d symbols: %s", len(target), target)
        log.info(
            "[DRY RUN] interval=%s from=%s to=%s data_dir=%s chunks=%d",
            config.interval,
            config.from_date.date(),
            config.to_date.date(),
            config.data_dir,
            len(chunks),
        )
        for i, (c_from, c_to) in enumerate(chunks, 1):
            log.info("[DRY RUN] Chunk %d: %s -> %s", i, c_from.date(), c_to.date())
        return DownloadResult(
            symbols_requested=target,
            symbols_downloaded=[],
            symbols_failed=[],
            symbols_missing_token=[],
            validation_reports={},
            file_paths={},
            dry_run=True,
            chunks=chunks,
        )

    # Fetch instrument list and build token map.
    log.info("Fetching instrument list for %s…", exchange_str)
    raw_instruments: list[dict[str, Any]] = broker.get_instruments(exchange_str)
    token_map, missing = build_instrument_map(raw_instruments, target, exchange=exchange_str)

    downloader = HistoricalDataDownloader(broker=broker, data_dir=config.data_dir, logger=log)

    downloaded: list[str] = []
    failed: list[str] = []
    validation_reports: dict[str, DataValidationReport] = {}
    file_paths: dict[str, Path] = {}

    for symbol in target:
        if symbol not in token_map:
            continue  # already recorded in missing

        try:
            # Download all chunks without saving intermediate results.
            chunk_dfs: list[pd.DataFrame] = []
            for chunk_from, chunk_to in chunks:
                time.sleep(0.4)  # Respect Zerodha 3 req/sec limit
                df_chunk, _ = downloader.download(
                    instrument_token=token_map[symbol],
                    symbol=symbol,
                    exchange=exchange,
                    interval=config.interval,
                    from_date=chunk_from,
                    to_date=chunk_to,
                    save=False,
                )
                if not df_chunk.empty:
                    chunk_dfs.append(df_chunk)

            # Concatenate all chunks into one DataFrame.
            if chunk_dfs:
                df_new = pd.concat(chunk_dfs, ignore_index=True)
            else:
                df_new = pd.DataFrame(
                    columns=["timestamp", "open", "high", "low", "close", "volume"]
                )

            # Merge with existing Parquet unless --replace.
            if config.save and not config.replace:
                existing_path = downloader.get_candle_file_path(symbol, exchange, config.interval)
                if existing_path.exists():
                    df_existing = pd.read_parquet(existing_path)
                    log.info(
                        "Merging %d new rows with %d existing rows for %s.",
                        len(df_new),
                        len(df_existing),
                        symbol,
                    )
                    df_new = pd.concat([df_existing, df_new], ignore_index=True)

            # Deduplicate by timestamp and sort ascending.
            df_new = (
                df_new.drop_duplicates(subset=["timestamp"])
                .sort_values("timestamp")
                .reset_index(drop=True)
            )

            # Validate the final merged result.
            report = validate_ohlcv_dataframe(df_new, symbol, exchange, config.interval)
            validation_reports[symbol] = report

            if report.is_valid:
                log.info("Validation OK for %s: %d rows.", symbol, report.row_count)
            else:
                error_codes = [i.code for i in report.issues if i.severity == "error"]
                log.warning("Validation errors for %s: %s", symbol, error_codes)

            # Save the merged DataFrame.
            if config.save:
                path = downloader.get_candle_file_path(symbol, exchange, config.interval)
                path.parent.mkdir(parents=True, exist_ok=True)
                df_new.to_parquet(path, index=False)
                file_paths[symbol] = path
                log.info("Saved %d candles → %s", len(df_new), path)

            downloaded.append(symbol)

        except Exception as exc:
            log.error("Failed to download %s: %s", symbol, exc)
            failed.append(symbol)

    return DownloadResult(
        symbols_requested=target,
        symbols_downloaded=downloaded,
        symbols_failed=failed,
        symbols_missing_token=missing,
        validation_reports=validation_reports,
        file_paths=file_paths,
        dry_run=False,
        chunks=chunks,
    )
