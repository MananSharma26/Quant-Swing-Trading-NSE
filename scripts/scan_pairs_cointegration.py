"""ADF Cointegration Scanner for Pairs Trading.

Tests every unique pair combination in the universe for statistical cointegration
using the Augmented Dickey-Fuller (ADF) test. 
To prevent memory blowouts and massive data loss from N-way inner joins, it loads 
and joins data pair-by-pair.

Usage:
    python scripts/scan_pairs_cointegration.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from itertools import combinations
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd
import numpy as np

try:
    import statsmodels.api as sm
    from statsmodels.tsa.stattools import adfuller
except ImportError:
    print("Error: statsmodels is not installed. Run 'pip install statsmodels'")
    sys.exit(1)

from trading_engine.data.universe import load_universe_config
from trading_engine.common.config import load_settings

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/nifty50.yaml", help="Path to universe config")
    parser.add_argument("--interval", default="minute", help="Candle interval to load")
    parser.add_argument("--output", default="reports/cointegrated_pairs.json", help="Output file")
    return parser.parse_args()

def load_symbol_data(symbol: str, data_dir: Path, interval: str) -> pd.DataFrame | None:
    path = data_dir / "candles" / "NSE" / symbol / f"{interval}.parquet"
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path, columns=["timestamp", "close"])
        df = df.rename(columns={"close": symbol})
        df = df.set_index("timestamp")
        return df
    except Exception:
        return None

def main() -> None:
    args = _parse_args()
    settings = load_settings()
    data_dir = Path(settings.data_dir)
    
    universe = load_universe_config(Path(args.config))
    symbols = universe.get_symbols()
    
    # Pre-load all individual series into a dict to save disk I/O
    print(f"Pre-loading data for {len(symbols)} symbols from disk...", flush=True)
    series_dict = {}
    for sym in symbols:
        df = load_symbol_data(sym, data_dir, args.interval)
        if df is not None and not df.empty:
            series_dict[sym] = df
            
    valid_symbols = list(series_dict.keys())
    print(f"Successfully loaded {len(valid_symbols)} valid symbols.", flush=True)
    
    out_path = Path(args.output).with_suffix('.jsonl')
    out_path.parent.mkdir(parents=True, exist_ok=True)
    
    processed_pairs = set()
    results = []
    if out_path.exists():
        with out_path.open("r") as f:
            for line in f:
                if not line.strip(): continue
                data = json.loads(line)
                processed_pairs.add((data["symbol_a"], data["symbol_b"]))
                results.append(data)
                
    if processed_pairs:
        print(f"Found {len(processed_pairs)} already processed pairs. Resuming...", flush=True)

    pairs = list(combinations(valid_symbols, 2))
    total_pairs = len(pairs)
    print(f"Scanning {total_pairs} unique pairs for cointegration...", flush=True)
    
    start_time = time.time()
    for i, (sym_a, sym_b) in enumerate(pairs, 1):
        if (sym_a, sym_b) in processed_pairs or (sym_b, sym_a) in processed_pairs:
            continue
            
        if i % 50 == 0:
            elapsed = time.time() - start_time
            print(f"  Processed {i}/{total_pairs} pairs... ({elapsed:.1f}s elapsed)", flush=True)
            
        # Inner join just these two symbols
        df_pair = series_dict[sym_a].join(series_dict[sym_b], how="inner").dropna()
        if len(df_pair) < 1000:
            continue # Skip if they have almost no overlapping data
            
        y = df_pair[sym_a].values
        x = df_pair[sym_b].values
        
        # OLS Regression
        x_with_const = sm.add_constant(x)
        model = sm.OLS(y, x_with_const).fit()
        hedge_ratio = model.params[1]
        
        spread = y - (hedge_ratio * x)
        
        try:
            # fast ADF
            adf_result = adfuller(spread, maxlag=1)
            p_value = adf_result[1]
            adf_stat = adfuller(spread)[0]
        except Exception:
            continue
            
        result_data = {
            "symbol_a": sym_a,
            "symbol_b": sym_b,
            "hedge_ratio": float(hedge_ratio),
            "p_value": float(p_value),
            "adf_stat": float(adf_stat)
        }
        results.append(result_data)
        
        # Incrementally save to disk
        with out_path.open("a") as f:
            f.write(json.dumps(result_data) + "\n")
        
    # Sort by lowest p-value at the very end to show top 15
    results.sort(key=lambda x: x["p_value"])
        
    print(f"\nSaved full results to {out_path}", flush=True)
    
    print("\n" + "="*60, flush=True)
    print("TOP 15 MOST COINTEGRATED PAIRS (p-value < 0.05 is required)", flush=True)
    print("="*60, flush=True)
    print(f"{'Pair':<25} | {'Hedge Ratio':<12} | {'p-value'}", flush=True)
    print("-" * 60, flush=True)
    
    for r in results[:15]:
        pair_name = f"{r['symbol_a']} / {r['symbol_b']}"
        print(f"{pair_name:<25} | {r['hedge_ratio']:>12.4f} | {r['p_value']:.6f}", flush=True)

if __name__ == "__main__":
    main()
