import sys
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

symbols = ["BAJAJFINSV"]

for symbol in symbols:
    in_path = DATA_DIR / "candles" / "NSE" / symbol / "minute.parquet"
    out_path = DATA_DIR / "candles" / "NSE" / symbol / "day.parquet"
    
    if not in_path.exists():
        print(f"Skipping {symbol}, no minute data found.")
        continue
        
    print(f"Resampling {symbol} from minute to daily...")
    df = pd.read_parquet(in_path)
    df = df.set_index("timestamp")
    
    # Resample to daily
    daily_df = df.resample("D").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum"
    }).dropna()
    
    daily_df = daily_df.reset_index()
    daily_df.to_parquet(out_path, index=False)
    print(f"  Saved {len(daily_df)} daily candles to {out_path}")
