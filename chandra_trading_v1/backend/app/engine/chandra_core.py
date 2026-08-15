"""
Chandra Trend Engine -> MT5 Auto Trader
========================================

Python implementation of the supplied TradingView Pine Script:
"Chandra Trend Engine"

Signal calculation:
    buyCondition  and sellCondition are calculated exactly from the Pine logic.

MT5 execution:
    Trading mode is selected with --signal-mode:
      buy_sell:
        BUY entry  = BuyCondition followed by Strategic BUY
        SELL entry = SellCondition followed by Strategic SELL
        BUY exit   = SellCondition becomes TRUE -> close BUY immediately
        SELL exit  = BuyCondition becomes TRUE -> close SELL immediately

      magical:
        BUY entry  = NEW Magical BUY event
        SELL entry = NEW Magical SELL event
        BUY exit   = later close below the Magical BUY signal candle LOW
        SELL exit  = later close above the Magical SELL signal candle HIGH

    All Pine signals are still calculated/displayed. Only the selected
    execution mode is allowed to trade.

    On startup, the current signal is used only as a baseline. The bot
    waits for the next signal event instead of entering immediately.

No time-cycle logic is used.

IMPORTANT:
    Default mode is DRY RUN. No live orders are sent unless --live is supplied.
"""

from __future__ import annotations

import argparse
import math
import sys
import time as time_module
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

try:
    import MetaTrader5 as mt5
except ImportError:
    print("ERROR: MetaTrader5 package is not installed.")
    print("Install with: pip install MetaTrader5 pandas numpy")
    sys.exit(1)


# ============================================================
# PINE SCRIPT CONSTANTS
# ============================================================

TRAIL_TYPE = "modified"
ATR_PERIOD = 28
ATR_FACTOR = 5
RSI_PERIOD = 14
RSI_OVERBOUGHT = 60
RSI_OVERSOLD = 40
EMA_PERIOD = 1000
FIB_LEVEL_3 = 88.6
FIB_LEVEL_4 = 10

ENABLE_BUY_SELL = True


# ============================================================
# DATA STRUCTURES
# ============================================================

@dataclass
class SignalState:
    position: int
    buy_condition: bool
    sell_condition: bool
    new_buy_signal: bool
    new_sell_signal: bool

    close: float
    norm_close: float
    supertrend: float
    l100: float
    rsi: float
    ema: float

    trend: int
    extreme_point: float
    fib3: float
    volume_line: float

    sunghamam: bool
    maha_sunghamam: bool

    g_buy: bool
    g_sell: bool
    fake_buy: bool
    fake_sell: bool

    strategic_buy: bool
    strategic_sell: bool

    magical_buy: bool
    magical_sell: bool
    magical_invalid: bool



@dataclass
class ExecutionState:
    mode: str
    initialized: bool = False
    # Unique MT5 magic number so multiple Chandra strategies can trade
    # the same symbol independently.
    magic_number: int = BOT_MAGIC_NUMBER if "BOT_MAGIC_NUMBER" in globals() else 26081201
    # Safety control: when False, exits/SL management still run, but no new entries are allowed.
    entries_enabled: bool = True

    # Base -> Strategic confirmation for buy_sell mode.
    buy_armed: bool = False
    sell_armed: bool = False

    # Last event state, used to avoid retriggering persistent signals.
    prev_buy_condition: bool = False
    prev_sell_condition: bool = False
    prev_strategic_buy: bool = False
    prev_strategic_sell: bool = False
    prev_magical_buy: bool = False
    prev_magical_sell: bool = False

    # Exit reference levels.
    buy_exit_low: float = np.nan
    sell_exit_high: float = np.nan

    # DRY RUN virtual position. Live mode uses real MT5 positions.
    virtual_position: int = 0

    # Last displayed signal signature; prevents repetitive candle-by-candle logs.
    last_log_signature = None
    last_status_signature = None


# ============================================================
# MT5 CONNECTION
# ============================================================

def connect_mt5():
    """
    Connect to the already-installed/running MT5 terminal.

    If MT5 is not running, initialize() may still find the terminal
    if the terminal installation is discoverable by the package.
    """
    if not mt5.initialize():
        code, message = mt5.last_error()
        raise RuntimeError(f"MT5 initialize failed: {code} - {message}")

    account = mt5.account_info()
    if account is None:
        raise RuntimeError(
            "MT5 connected but no trading account is logged in. "
            "Open MT5 and login to your Equiti account."
        )

    print("=" * 72)
    print("MT5 CONNECTED")
    print(f"Login       : {account.login}")
    print(f"Server      : {account.server}")
    print(f"Company     : {account.company}")
    print(f"Balance     : {account.balance}")
    print(f"Equity      : {account.equity}")
    print("=" * 72)


def ensure_symbol(symbol: str):
    info = mt5.symbol_info(symbol)

    if info is None:
        # Symbol not found under this exact name. Brokers often use
        # suffixes/prefixes (e.g. XAUUSD.m, XAUUSDm, GOLDs). Search all
        # available symbols for likely matches to help the user find the
        # correct name instead of just failing blind.
        all_symbols = mt5.symbols_get()
        core = symbol.upper().replace("USD", "").replace("/", "")
        matches = []
        if all_symbols:
            for s in all_symbols:
                name_upper = s.name.upper()
                if symbol.upper() in name_upper or core in name_upper:
                    matches.append(s.name)

        # Rank matches: symbols that START WITH the exact requested name
        # (e.g. "XAUUSD.sd" for "XAUUSD") are almost always the correct
        # one. Symbols that merely contain the core commodity code
        # (e.g. "BTCXAU.lv") are usually unrelated crosses and should be
        # ranked lower, not suggested first.
        def match_rank(name: str):
            name_upper = name.upper()
            if name_upper.startswith(symbol.upper()):
                return 0
            if name_upper == symbol.upper():
                return -1
            return 1

        matches = sorted(set(matches), key=lambda n: (match_rank(n), n))

        msg = (
            f"Symbol '{symbol}' was not found in MT5 under that exact name.\n"
            f"This usually means the broker uses a different suffix/prefix."
        )
        if matches:
            msg += (
                "\nPossible matches found on this account (best match first):\n  "
                + "\n  ".join(matches[:20])
                + f"\n\nTry re-running with: --symbol {matches[0]}"
            )
        else:
            msg += (
                "\nNo similar symbols found automatically. Open MT5's "
                "Market Watch, right-click -> 'Show All', and search "
                "manually for the correct gold symbol name."
            )
        raise RuntimeError(msg)

    if not info.visible:
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"Could not select symbol '{symbol}'")

    return info


# ============================================================
# MT5 DATA
# ============================================================

def timeframe_to_mt5(tf: str):
    tf = str(tf).upper().strip()

    mapping = {
        "1": mt5.TIMEFRAME_M1,
        "M1": mt5.TIMEFRAME_M1,
        "3": mt5.TIMEFRAME_M3,
        "M3": mt5.TIMEFRAME_M3,
        "5": mt5.TIMEFRAME_M5,
        "M5": mt5.TIMEFRAME_M5,
        "15": mt5.TIMEFRAME_M15,
        "M15": mt5.TIMEFRAME_M15,
        "30": mt5.TIMEFRAME_M30,
        "M30": mt5.TIMEFRAME_M30,
        "60": mt5.TIMEFRAME_H1,
        "H1": mt5.TIMEFRAME_H1,
        "240": mt5.TIMEFRAME_H4,
        "H4": mt5.TIMEFRAME_H4,
        "D": mt5.TIMEFRAME_D1,
        "D1": mt5.TIMEFRAME_D1,
    }

    if tf not in mapping:
        raise ValueError(f"Unsupported timeframe: {tf}")

    return mapping[tf]


def timeframe_minutes(tf: str) -> int:
    tf = str(tf).upper().strip()

    mapping = {
        "1": 1, "M1": 1,
        "3": 3, "M3": 3,
        "5": 5, "M5": 5,
        "15": 15, "M15": 15,
        "30": 30, "M30": 30,
        "60": 60, "H1": 60,
        "240": 240, "H4": 240,
        "D": 1440, "D1": 1440,
    }

    if tf not in mapping:
        raise ValueError(f"Unsupported timeframe: {tf}")

    return mapping[tf]


def get_rates_range(symbol: str, tf: str, start_dt, end_dt) -> pd.DataFrame:
    """Fetch closed MT5 candles for an explicit UTC date/time range."""
    mt5_tf = timeframe_to_mt5(tf)
    rates = mt5.copy_rates_range(symbol, mt5_tf, start_dt, end_dt)
    if rates is None or len(rates) == 0:
        code, message = mt5.last_error()
        raise RuntimeError(f"No MT5 data: {code} - {message}")
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    # Exclude the currently forming candle.
    if len(df) > 1:
        df = df.iloc[:-1].copy()
    df.reset_index(drop=True, inplace=True)
    return df


def get_rates(symbol: str, tf: str, bars: int = 3000) -> pd.DataFrame:
    mt5_tf = timeframe_to_mt5(tf)

    rates = mt5.copy_rates_from_pos(symbol, mt5_tf, 0, bars)

    if rates is None or len(rates) == 0:
        code, message = mt5.last_error()
        raise RuntimeError(f"No MT5 data: {code} - {message}")

    df = pd.DataFrame(rates)

    df["time"] = pd.to_datetime(df["time"], unit="s")

    # Pine's current bar is not used for a once-per-bar-close strategy.
    # The last MT5 candle is normally the currently forming candle.
    if len(df) > 1:
        df = df.iloc[:-1].copy()

    df.reset_index(drop=True, inplace=True)

    return df


# ============================================================
# PINE-COMPATIBLE INDICATOR FUNCTIONS
# ============================================================

def wilders_ma(src: pd.Series, length: int) -> pd.Series:
    """
    Exact equivalent of the custom Pine function:

        var float wildMA = na
        wildMA := na(wildMA[1]) ? src : wildMA[1] + (src - wildMA[1]) / length

    This is NOT initialized with an SMA. The first valid source value
    becomes the initial Wilder value.
    """
    values = src.to_numpy(dtype=float)
    out = np.full(len(values), np.nan, dtype=float)

    previous = np.nan

    for i, value in enumerate(values):
        if np.isnan(value):
            continue

        if np.isnan(previous):
            previous = value
        else:
            previous = previous + (value - previous) / length

        out[i] = previous

    return pd.Series(out, index=src.index)


def pine_sma(src: pd.Series, length: int) -> pd.Series:
    return src.rolling(length, min_periods=length).mean()


def modified_true_range(df: pd.DataFrame) -> pd.Series:
    """
    Exact Pine calculateTrueRange():

        hiLo = math.min(high-low, 1.5 * ta.sma(high-low, ATR_PERIOD))

        hRef = low <= high[1]
               ? high-close[1]
               : high-close[1] - 0.5*(low-high[1])

        lRef = high >= low[1]
               ? close[1]-low
               : close[1]-low - 0.5*(low[1]-high)

        modified = max(hiLo, hRef, lRef)
    """

    high = df["high"]
    low = df["low"]
    close = df["close"]

    range_raw = high - low
    avg_range = pine_sma(range_raw, ATR_PERIOD)

    hi_lo = pd.concat(
        [range_raw, 1.5 * avg_range],
        axis=1
    ).min(axis=1, skipna=False)

    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    h_ref = np.where(
        low <= prev_high,
        high - prev_close,
        high - prev_close - 0.5 * (low - prev_high)
    )

    l_ref = np.where(
        high >= prev_low,
        prev_close - low,
        prev_close - low - 0.5 * (prev_low - high)
    )

    tr = pd.concat(
        [
            hi_lo,
            pd.Series(h_ref, index=df.index),
            pd.Series(l_ref, index=df.index),
        ],
        axis=1
    ).max(axis=1, skipna=False)

    return tr


def calculate_supertrend(df: pd.DataFrame):
    """
    Reproduces the Pine custom Supertrend:

        trueRange = calculateTrueRange()
        atrLoss = ATR_FACTOR * wildersMA(trueRange, ATR_PERIOD)

        upperBand = close - atrLoss
        lowerBand = close + atrLoss

        trendUp := close[1] > trendUp[1]
                     ? max(upperBand, trendUp[1])
                     : upperBand

        trendDown := close[1] < trendDown[1]
                       ? min(lowerBand, trendDown[1])
                       : lowerBand

        trend := close > trendDown[1]
                    ? 1
                    : close < trendUp[1]
                        ? -1
                        : nz(trend[1], 1)

        supertrend = trend == 1 ? trendUp : trendDown
    """

    close = df["close"]

    true_range = modified_true_range(df)
    atr_loss = ATR_FACTOR * wilders_ma(true_range, ATR_PERIOD)

    upper_band = close - atr_loss
    lower_band = close + atr_loss

    n = len(df)

    trend_up = np.full(n, np.nan)
    trend_down = np.full(n, np.nan)
    trend = np.ones(n, dtype=int)
    supertrend = np.full(n, np.nan)

    for i in range(n):
        if i == 0:
            trend_up[i] = upper_band.iloc[i]
            trend_down[i] = lower_band.iloc[i]
            trend[i] = 1
            supertrend[i] = trend_up[i]
            continue

        prev_close = close.iloc[i - 1]

        prev_up = trend_up[i - 1]
        prev_down = trend_down[i - 1]

        ub = upper_band.iloc[i]
        lb = lower_band.iloc[i]

        if np.isnan(ub):
            trend_up[i] = np.nan
        elif not np.isnan(prev_up) and prev_close > prev_up:
            trend_up[i] = max(ub, prev_up)
        else:
            trend_up[i] = ub

        if np.isnan(lb):
            trend_down[i] = np.nan
        elif not np.isnan(prev_down) and prev_close < prev_down:
            trend_down[i] = min(lb, prev_down)
        else:
            trend_down[i] = lb

        # Pine:
        # trend := close > trendDown[1] ? 1 :
        #          close < trendUp[1] ? -1 :
        #          nz(trend[1], 1)

        if not np.isnan(prev_down) and close.iloc[i] > prev_down:
            trend[i] = 1
        elif not np.isnan(prev_up) and close.iloc[i] < prev_up:
            trend[i] = -1
        else:
            trend[i] = trend[i - 1] if i > 0 else 1

        supertrend[i] = (
            trend_up[i] if trend[i] == 1 else trend_down[i]
        )

    return (
        true_range,
        atr_loss,
        pd.Series(trend_up, index=df.index),
        pd.Series(trend_down, index=df.index),
        pd.Series(trend, index=df.index),
        pd.Series(supertrend, index=df.index),
    )


def pine_rsi(close: pd.Series, length: int) -> pd.Series:
    """
    TradingView ta.rsi() is based on Wilder/RMA average gains/losses.

    RMA initialization uses the first length-value SMA.
    """
    delta = close.diff()

    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    def rma_pine(series: pd.Series, period: int) -> pd.Series:
        x = series.to_numpy(dtype=float)
        out = np.full(len(x), np.nan)

        valid = np.where(~np.isnan(x))[0]

        if len(valid) < period:
            return pd.Series(out, index=series.index)

        seed_idx = valid[period - 1]
        seed = np.mean(x[valid[:period]])

        out[seed_idx] = seed
        prev = seed

        for i in range(seed_idx + 1, len(x)):
            if np.isnan(x[i]):
                out[i] = np.nan
                continue

            prev = ((prev * (period - 1)) + x[i]) / period
            out[i] = prev

        return pd.Series(out, index=series.index)

    avg_gain = rma_pine(gain, length)
    avg_loss = rma_pine(loss, length)

    rs = avg_gain / avg_loss

    rsi = 100.0 - (100.0 / (1.0 + rs))

    # Pine behavior for zero loss / zero gain.
    rsi = rsi.where(avg_loss != 0, 100.0)
    rsi = rsi.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)

    return rsi


def pine_ema(src: pd.Series, length: int) -> pd.Series:
    """
    TradingView EMA uses alpha = 2/(length+1).
    Seed is the SMA of the first length valid values.
    """
    x = src.to_numpy(dtype=float)
    out = np.full(len(x), np.nan)

    valid = np.where(~np.isnan(x))[0]

    if len(valid) < length:
        return pd.Series(out, index=src.index)

    seed_idx = valid[length - 1]
    seed = np.mean(x[valid[:length]])

    out[seed_idx] = seed

    alpha = 2.0 / (length + 1.0)
    prev = seed

    for i in range(seed_idx + 1, len(x)):
        if np.isnan(x[i]):
            out[i] = np.nan
            continue

        prev = alpha * x[i] + (1.0 - alpha) * prev
        out[i] = prev

    return pd.Series(out, index=src.index)


def pine_vwma(close: pd.Series, volume: pd.Series, length: int) -> pd.Series:
    pv = close * volume
    return pv.rolling(length, min_periods=length).sum() / volume.rolling(
        length, min_periods=length
    ).sum()


# ============================================================
# FULL PINE ENGINE
# ============================================================

def calculate_pine_engine(df: pd.DataFrame):
    """
    Calculates the complete non-MTF logic from the supplied Pine script.

    MTF display table is intentionally not used for trading because
    the requested MT5 strategy trades the selected timeframe only.
    """

    df = df.copy()

    # Pine normalizedTicker points to the broker's normalized symbol.
    # For MT5 this is simply the selected symbol's OHLC data.
    df["normOpen"] = df["open"]
    df["normHigh"] = df["high"]
    df["normLow"] = df["low"]
    df["normClose"] = df["close"]

    (
        df["trueRange"],
        df["atrLoss"],
        df["trendUp"],
        df["trendDown"],
        df["trend"],
        df["supertrend"],
    ) = calculate_supertrend(df)

    # Pine:
    # extremePoint := crossUp ? normHigh :
    #                  crossDown ? normLow :
    #                  trend == 1 ? max(extremePoint[1], normHigh) :
    #                  trend == -1 ? min(extremePoint[1], normLow) :
    #                  extremePoint[1]
    extreme = np.full(len(df), np.nan)

    previous_trend = 1

    for i in range(len(df)):
        t = int(df["trend"].iloc[i])

        # Pine ta.crossover(trend, 0)
        cross_up = previous_trend <= 0 and t > 0

        # Pine ta.crossunder(trend, 0)
        cross_down = previous_trend >= 0 and t < 0

        high = df["normHigh"].iloc[i]
        low = df["normLow"].iloc[i]

        if cross_up:
            extreme[i] = high
        elif cross_down:
            extreme[i] = low
        elif t == 1:
            if i > 0 and not np.isnan(extreme[i - 1]):
                extreme[i] = max(extreme[i - 1], high)
            else:
                extreme[i] = high
        elif t == -1:
            if i > 0 and not np.isnan(extreme[i - 1]):
                extreme[i] = min(extreme[i - 1], low)
            else:
                extreme[i] = low
        else:
            extreme[i] = extreme[i - 1] if i > 0 else np.nan

        previous_trend = t

    df["extremePoint"] = extreme

    df["fibRange"] = df["supertrend"] - df["extremePoint"]
    df["fib2"] = (
        df["extremePoint"] +
        df["fibRange"] * 78.6 / 100.0
    )
    df["fib3"] = (
        df["extremePoint"] +
        df["fibRange"] * FIB_LEVEL_3 / 100.0
    )
    df["fib4"] = (
        df["extremePoint"] +
        df["fibRange"] * FIB_LEVEL_4 / 100.0
    )

    df["l100"] = df["supertrend"]

    # Technical indicators
    df["rsi"] = pine_rsi(df["close"], RSI_PERIOD)
    df["emaValue"] = pine_ema(df["close"], EMA_PERIOD)

    # Pine ta.vwma(close, 112)
    df["volumeLine"] = pine_vwma(
        df["close"],
        df["tick_volume"],
        112
    )

    # --------------------------------------------------------
    # SIGNAL STATE
    # --------------------------------------------------------
    position = np.zeros(len(df), dtype=int)

    new_buy = np.zeros(len(df), dtype=bool)
    new_sell = np.zeros(len(df), dtype=bool)

    buy_condition_arr = np.zeros(len(df), dtype=bool)
    sell_condition_arr = np.zeros(len(df), dtype=bool)

    rsi_up_arr = np.zeros(len(df), dtype=bool)
    rsi_down_arr = np.zeros(len(df), dtype=bool)

    # Other Pine state
    buy_signal_high = np.full(len(df), np.nan)
    buy_signal_low = np.full(len(df), np.nan)
    sell_signal_high = np.full(len(df), np.nan)
    sell_signal_low = np.full(len(df), np.nan)

    g_buy = np.zeros(len(df), dtype=bool)
    g_sell = np.zeros(len(df), dtype=bool)
    fake_buy = np.zeros(len(df), dtype=bool)
    fake_sell = np.zeros(len(df), dtype=bool)

    strategic_buy = np.zeros(len(df), dtype=bool)
    strategic_sell = np.zeros(len(df), dtype=bool)

    sunghamam = np.zeros(len(df), dtype=bool)
    maha_sunghamam = np.zeros(len(df), dtype=bool)

    magical_buy = np.zeros(len(df), dtype=bool)
    magical_sell = np.zeros(len(df), dtype=bool)
    magical_invalid = np.zeros(len(df), dtype=bool)

    # Sunghamam state
    sunghamam_triggered = False
    maha_sunghamam_triggered = False

    # G/Fake state
    g_buy_triggered = False
    g_sell_triggered = False
    fake_buy_triggered = False
    fake_sell_triggered = False
    buy_follow_up_completed = False
    sell_follow_up_completed = False

    # Strategic state is evaluated from each confirmed candle.

    # Magical Candle state
    sig_high = np.nan
    sig_low = np.nan
    sig_dir = 0
    sig_bar_idx = -1

    high_broken = False
    low_broken = False
    magical = False
    invalid = False

    current_position = 0

    for i in range(len(df)):
        close = float(df["close"].iloc[i])
        high = float(df["high"].iloc[i])
        low = float(df["low"].iloc[i])

        norm_close = float(df["normClose"].iloc[i])
        l100 = float(df["l100"].iloc[i]) if not np.isnan(df["l100"].iloc[i]) else np.nan
        rsi = float(df["rsi"].iloc[i]) if not np.isnan(df["rsi"].iloc[i]) else np.nan
        ema = float(df["emaValue"].iloc[i]) if not np.isnan(df["emaValue"].iloc[i]) else np.nan

        # Default current position carries forward.
        position[i] = current_position

        # Pine:
        # rsi_up = rsi >= 60
        # rsi_down = rsi <= 40
        rsi_up = not np.isnan(rsi) and rsi >= RSI_OVERBOUGHT
        rsi_down = not np.isnan(rsi) and rsi <= RSI_OVERSOLD

        rsi_up_arr[i] = rsi_up
        rsi_down_arr[i] = rsi_down

        # Pine:
        # buyCondition = (normClose >= l100 and rsi_up)
        #             or (normClose >= l100 and close > emaValue)
        #
        # NaN comparisons evaluate as false for our trading purposes.
        buy_condition = (
            not np.isnan(l100)
            and norm_close >= l100
            and (
                rsi_up
                or (not np.isnan(ema) and close > ema)
            )
        )

        sell_condition = (
            not np.isnan(l100)
            and norm_close <= l100
            and (
                rsi_down
                or (not np.isnan(ema) and close < ema)
            )
        )

        buy_condition_arr[i] = buy_condition
        sell_condition_arr[i] = sell_condition

        # Pine:
        # newBuySignal = buyCondition and position != 1
        # newSellSignal = sellCondition and position != -1
        new_buy_signal = buy_condition and current_position != 1
        new_sell_signal = sell_condition and current_position != -1

        new_buy[i] = new_buy_signal
        new_sell[i] = new_sell_signal

        # Pine position update
        if new_buy_signal:
            current_position = 1
        elif new_sell_signal:
            current_position = -1

        position[i] = current_position

        # ----------------------------------------------------
        # G-BUY / FAKE-BUY / G-SELL / FAKE-SELL
        # ----------------------------------------------------
        if new_buy_signal:
            buy_signal_high[i] = high
            buy_signal_low[i] = low

            g_buy_triggered = False
            fake_buy_triggered = False
            buy_follow_up_completed = False
            sell_follow_up_completed = True

        else:
            if i > 0:
                buy_signal_high[i] = buy_signal_high[i - 1]
                buy_signal_low[i] = buy_signal_low[i - 1]

        if new_sell_signal:
            sell_signal_high[i] = high
            sell_signal_low[i] = low

            g_sell_triggered = False
            fake_sell_triggered = False
            sell_follow_up_completed = False
            buy_follow_up_completed = True

        else:
            if i > 0:
                sell_signal_high[i] = sell_signal_high[i - 1]
                sell_signal_low[i] = sell_signal_low[i - 1]

        g_buy_condition = (
            current_position == 1
            and not g_buy_triggered
            and not buy_follow_up_completed
            and not np.isnan(buy_signal_high[i])
            and close > buy_signal_high[i]
        )

        g_sell_condition = (
            current_position == -1
            and not g_sell_triggered
            and not sell_follow_up_completed
            and not np.isnan(sell_signal_low[i])
            and close < sell_signal_low[i]
        )

        if g_buy_condition:
            g_buy_triggered = True
            buy_follow_up_completed = True

        if g_sell_condition:
            g_sell_triggered = True
            sell_follow_up_completed = True

        fake_buy_condition = (
            current_position == 1
            and not fake_buy_triggered
            and not buy_follow_up_completed
            and not np.isnan(buy_signal_low[i])
            and close < buy_signal_low[i]
        )

        fake_sell_condition = (
            current_position == -1
            and not fake_sell_triggered
            and not sell_follow_up_completed
            and not np.isnan(sell_signal_high[i])
            and close > sell_signal_high[i]
        )

        if fake_buy_condition:
            fake_buy_triggered = True
            buy_follow_up_completed = True

        if fake_sell_condition:
            fake_sell_triggered = True
            sell_follow_up_completed = True

        g_buy[i] = g_buy_condition
        g_sell[i] = g_sell_condition
        fake_buy[i] = fake_buy_condition
        fake_sell[i] = fake_sell_condition

        # ----------------------------------------------------
        # SUNGHAMAM / MAHA SUNGHAMAM
        # ----------------------------------------------------
        fib3 = df["fib3"].iloc[i]
        volume_line = df["volumeLine"].iloc[i]

        if (
            not np.isnan(l100)
            and not np.isnan(fib3)
            and not np.isnan(volume_line)
        ):
            upper_bound = max(l100, fib3)
            lower_bound = min(l100, fib3)

            volume_inside = (
                volume_line > lower_bound
                and volume_line < upper_bound
            )

            sunghamam_condition = (
                volume_inside
                and not sunghamam_triggered
            )

            if sunghamam_condition:
                sunghamam_triggered = True

            if not volume_inside:
                sunghamam_triggered = False

            ema_inside = (
                not np.isnan(ema)
                and ema > lower_bound
                and ema < upper_bound
            )

            maha_condition = (
                volume_inside
                and ema_inside
                and not maha_sunghamam_triggered
            )

            if maha_condition:
                maha_sunghamam_triggered = True

            if not (volume_inside and ema_inside):
                maha_sunghamam_triggered = False

            sunghamam[i] = sunghamam_condition
            maha_sunghamam[i] = maha_condition

        # ----------------------------------------------------
        # STRATEGIC BUY / SELL
        # ----------------------------------------------------
        # Confirmed retest rule (same candle):
        #
        # BUY setup active
        #   -> candle LOW touches/crosses Fib 3
        #   -> candle CLOSES ABOVE Fib 3
        #   -> Strategic BUY
        #
        # SELL setup active
        #   -> candle HIGH touches/crosses Fib 3
        #   -> candle CLOSES BELOW Fib 3
        #   -> Strategic SELL
        #
        # This deliberately does NOT use ta.crossunder/ta.crossover.
        # A wick touch is enough to qualify, but the candle must finish on
        # the correct side of the level. This avoids false entries where
        # price merely touches the level and continues through it.
        if current_position == 1 and not np.isnan(fib3):
            strategic_buy[i] = (
                low <= fib3
                and close > fib3
            )
        elif current_position == -1 and not np.isnan(fib3):
            strategic_sell[i] = (
                high >= fib3
                and close < fib3
            )

        # ----------------------------------------------------
        # MAGICAL CANDLE - FIRST BREAK WINS
        # ----------------------------------------------------
        if new_buy_signal:
            sig_high = high
            sig_low = low
            sig_dir = 1
            sig_bar_idx = i

            high_broken = False
            low_broken = False
            magical = False
            invalid = False

        if new_sell_signal:
            sig_high = high
            sig_low = low
            sig_dir = -1
            sig_bar_idx = i

            high_broken = False
            low_broken = False
            magical = False
            invalid = False

        if (
            sig_dir != 0
            and not magical
            and not invalid
            and i > sig_bar_idx
        ):
            if high > sig_high and not high_broken and not low_broken:
                high_broken = True

            if low < sig_low and not low_broken and not high_broken:
                low_broken = True

        bullish_magical = sig_dir == 1 and low_broken
        bullish_invalid = sig_dir == 1 and high_broken

        bearish_magical = sig_dir == -1 and high_broken
        bearish_invalid = sig_dir == -1 and low_broken

        if bullish_magical or bearish_magical:
            magical = True

        if bullish_invalid or bearish_invalid:
            invalid = True

        magical_buy[i] = bearish_magical
        magical_sell[i] = bullish_magical
        magical_invalid[i] = bullish_invalid or bearish_invalid

    # Store arrays
    df["position"] = position
    df["buyCondition"] = buy_condition_arr
    df["sellCondition"] = sell_condition_arr
    df["newBuySignal"] = new_buy
    df["newSellSignal"] = new_sell

    df["rsi_up"] = rsi_up_arr
    df["rsi_down"] = rsi_down_arr

    df["gBuyCondition"] = g_buy
    df["gSellCondition"] = g_sell
    df["fakeBuyCondition"] = fake_buy
    df["fakeSellCondition"] = fake_sell

    df["sunghamam"] = sunghamam
    df["mahaSunghamam"] = maha_sunghamam

    df["strategicBuy"] = strategic_buy
    df["strategicSell"] = strategic_sell

    df["magicalBuy"] = magical_buy
    df["magicalSell"] = magical_sell
    df["magicalInvalid"] = magical_invalid

    return df


# ============================================================
# MT5 POSITION FUNCTIONS
# ============================================================

BOT_MAGIC_NUMBER = 26081201

# Set once in main() from CLI args, read by _open_direction() at entry time.
# Avoids threading entry-mode params through every _enter()/_open_direction()
# call site in the strategy logic.
ENTRY_CONFIG = {
    "mode": "market",          # "market" or "limit"
    "limit_wait_seconds": 2.5,
    "limit_offset_points": 5,
}

# Stop loss tracking, driven by the L100 (supertrend) line.
#   BUY  SL = L100 - offset_points  (L100 sits below price as support)
#   SELL SL = L100 + offset_points  (L100 sits above price as resistance)
# Set once in main() from CLI args; read every candle so the SL can be
# re-modified as L100 moves, not just set once at entry.
STOPLOSS_CONFIG = {
    "enabled": False,
    "offset_points": 5.0,
}


def compute_l100_stop_price(direction: str, l100: float, symbol_info) -> float | None:
    """
    Compute the SL price for a given direction from the current L100 value.
    Returns None if l100 is not a valid number.
    """
    if l100 is None or np.isnan(l100):
        return None

    point = symbol_info.point
    digits = symbol_info.digits
    offset = STOPLOSS_CONFIG["offset_points"] * point

    if direction.upper() == "BUY":
        return round(l100 - offset, digits)
    elif direction.upper() == "SELL":
        return round(l100 + offset, digits)
    else:
        raise ValueError(direction)


def modify_position_sl(position, new_sl: float, symbol_info) -> bool:
    """
    Modify the SL of an existing open position (keeps TP untouched).
    Skips the request entirely if new_sl is already the current SL
    (within half a point) to avoid spamming the broker every candle.
    """
    point = symbol_info.point
    current_sl = position.sl or 0.0

    if abs(current_sl - new_sl) < (point * 0.5):
        return True  # already correct, nothing to do

    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": position.symbol,
        "position": position.ticket,
        "sl": new_sl,
        "tp": position.tp,  # preserve existing TP (0.0 if none set)
    }

    result = mt5.order_send(request)

    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        retcode = result.retcode if result else None
        comment = result.comment if result else mt5.last_error()
        print(
            f"WARNING: could not modify SL for ticket={position.ticket} "
            f"to {new_sl} (retcode={retcode}, comment={comment})"
        )
        return False

    print(f"SL UPDATED: ticket={position.ticket} -> {new_sl} (L100-based)")
    return True


def sync_l100_stoploss(symbol: str, l100: float, magic_number: int = BOT_MAGIC_NUMBER):
    """
    Called once per processed candle. If L100-based SL tracking is enabled,
    recompute the target SL for every bot-owned open position (BUY and
    SELL) and modify it on the broker if it has moved.
    """
    if not STOPLOSS_CONFIG["enabled"]:
        return

    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        return

    for position in get_buy_positions(symbol, magic_number):
        new_sl = compute_l100_stop_price("BUY", l100, symbol_info)
        if new_sl is not None:
            modify_position_sl(position, new_sl, symbol_info)

    for position in get_sell_positions(symbol, magic_number):
        new_sl = compute_l100_stop_price("SELL", l100, symbol_info)
        if new_sl is not None:
            modify_position_sl(position, new_sl, symbol_info)


def get_positions(symbol: str, magic_number: int = BOT_MAGIC_NUMBER):
    """
    Return only positions opened BY THIS BOT (matching magic number).

    This deliberately excludes any manually-placed trades on the same
    symbol/account, so the bot's exit logic never touches a position you
    opened yourself in the MT5 terminal.
    """
    positions = mt5.positions_get(symbol=symbol)

    if positions is None:
        return []

    return [p for p in positions if p.magic == magic_number]


def get_buy_positions(symbol: str, magic_number: int = BOT_MAGIC_NUMBER):
    return [
        p for p in get_positions(symbol, magic_number)
        if p.type == mt5.POSITION_TYPE_BUY
    ]


def get_sell_positions(symbol: str, magic_number: int = BOT_MAGIC_NUMBER):
    return [
        p for p in get_positions(symbol, magic_number)
        if p.type == mt5.POSITION_TYPE_SELL
    ]


def choose_filling_mode(symbol_info):
    """
    Best-guess broker-supported filling mode, used as the FIRST attempt.

    Some brokers (StarTrader included) report symbol_info.filling_mode in a
    way that does not reliably predict what order_send() will actually
    accept. send_order_with_fallback() below is the authoritative path: it
    tries this guess first, then retries with the other filling modes if the
    broker rejects with "Unsupported filling mode" (retcode 10030).
    """
    mode = getattr(symbol_info, "filling_mode", None)

    if mode is not None:
        if mode & mt5.ORDER_FILLING_FOK:
            return mt5.ORDER_FILLING_FOK

        if mode & mt5.ORDER_FILLING_IOC:
            return mt5.ORDER_FILLING_IOC

    return mt5.ORDER_FILLING_RETURN


def send_order_with_fallback(request: dict):
    """
    Send an order, and if the broker rejects it specifically for filling
    mode (retcode 10030 / "Unsupported filling mode"), retry the SAME
    request with each of the other filling modes in turn until one is
    accepted or all have been tried.

    This is more reliable than guessing the filling mode from symbol_info
    up front, since some brokers report that field inaccurately.
    """
    tried = []
    candidates = [
        request.get("type_filling", mt5.ORDER_FILLING_FOK),
        mt5.ORDER_FILLING_FOK,
        mt5.ORDER_FILLING_IOC,
        mt5.ORDER_FILLING_RETURN,
    ]

    # De-duplicate while preserving order.
    seen = set()
    ordered_candidates = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            ordered_candidates.append(c)

    for filling_mode in ordered_candidates:
        req = dict(request)
        req["type_filling"] = filling_mode

        result = mt5.order_send(req)
        tried.append(filling_mode)

        if result is None:
            # Connection-level failure; no point retrying other fill modes.
            return result, tried

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            return result, tried

        if result.retcode != mt5.TRADE_RETCODE_INVALID_FILL:
            # A different error (margin, price, permissions, etc).
            # Retrying with a different filling mode will not help.
            return result, tried

        print(
            f"Filling mode {filling_mode} rejected "
            f"(retcode=10030). Trying next mode..."
        )

    # All candidates exhausted; return the last result we got.
    return result, tried


def normalize_volume(symbol_info, volume: float) -> float:
    min_volume = symbol_info.volume_min
    max_volume = symbol_info.volume_max
    step = symbol_info.volume_step

    volume = max(min_volume, min(max_volume, volume))

    if step > 0:
        volume = math.floor(volume / step + 1e-9) * step

    # Avoid floating point artifacts.
    decimals = max(0, int(round(-math.log10(step)))) if step < 1 else 0

    return round(volume, decimals)


def close_position(position, symbol_info, live: bool, magic_number: int = BOT_MAGIC_NUMBER):
    symbol = position.symbol

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        print(f"ERROR: No tick for {symbol}")
        return False

    if position.type == mt5.POSITION_TYPE_BUY:
        order_type = mt5.ORDER_TYPE_SELL
        price = tick.bid
        action_name = "CLOSE BUY"
    else:
        order_type = mt5.ORDER_TYPE_BUY
        price = tick.ask
        action_name = "CLOSE SELL"

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": position.volume,
        "type": order_type,
        "position": position.ticket,
        "price": price,
        "deviation": 30,
        "magic": magic_number,
        "comment": "ChandraTrend-CLOSE",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": choose_filling_mode(symbol_info),
    }

    print(
        f"{action_name}: ticket={position.ticket} "
        f"volume={position.volume} price={price}"
    )

    if not live:
        print("DRY RUN -> close not sent")
        return True

    result, tried_modes = send_order_with_fallback(request)

    if result is None:
        print(f"ERROR: order_send returned None: {mt5.last_error()}")
        return False

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        print(
            f"ERROR closing position {position.ticket}: "
            f"retcode={result.retcode}, comment={result.comment} "
            f"(filling modes tried: {tried_modes})"
        )
        return False

    print(
        f"CLOSED: ticket={position.ticket}, "
        f"deal={result.deal}"
    )

    return True


def open_position(symbol: str, direction: str, volume: float, live: bool, sl: float | None = None, tp: float | None = None, magic_number: int = BOT_MAGIC_NUMBER):
    symbol_info = ensure_symbol(symbol)
    tick = mt5.symbol_info_tick(symbol)

    if tick is None:
        print(f"ERROR: No market tick for {symbol}")
        return False

    volume = normalize_volume(symbol_info, volume)

    if direction.upper() == "BUY":
        order_type = mt5.ORDER_TYPE_BUY
        price = tick.ask
    elif direction.upper() == "SELL":
        order_type = mt5.ORDER_TYPE_SELL
        price = tick.bid
    else:
        raise ValueError(direction)

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "price": price,
        "deviation": 30,
        "magic": magic_number,
        "comment": "ChandraTrend",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": choose_filling_mode(symbol_info),
    }

    if sl is not None:
        request["sl"] = sl
    if tp is not None:
        request["tp"] = tp

    print(
        f"OPEN {direction}: symbol={symbol} "
        f"volume={volume} price={price}"
        + (f" sl={sl}" if sl is not None else "")
    )

    if not live:
        print("DRY RUN -> order not sent")
        return True

    result, tried_modes = send_order_with_fallback(request)

    if result is None:
        print(f"ERROR: order_send returned None: {mt5.last_error()}")
        return False

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        print(
            f"ERROR opening {direction}: "
            f"retcode={result.retcode}, comment={result.comment} "
            f"(filling modes tried: {tried_modes})"
        )
        return False

    print(
        f"OPENED {direction}: "
        f"order={result.order}, deal={result.deal}"
    )

    return True


def open_position_limit_then_market(
    symbol: str,
    direction: str,
    volume: float,
    live: bool,
    wait_seconds: float = 2.5,
    offset_points: float = 5,
    sl: float | None = None,
    magic_number: int = BOT_MAGIC_NUMBER,
):
    """
    Try to enter with a LIMIT order priced slightly better than the current
    market (small offset), wait up to `wait_seconds` for it to fill, and if
    it hasn't filled by then, cancel it and fall back to a normal market
    order via open_position().

    Note: brokers require pending orders to sit a minimum distance from the
    current price (the symbol's "stops level" / freeze level) -- a limit
    order placed exactly AT the market price will typically be rejected.
    `offset_points` is combined with the broker's own minimum distance to
    pick a valid, safe price.

    Exits should NOT use this function -- always use close_position()
    (market) for exits, since certainty of getting out matters more than
    a small price improvement.
    """
    if not live:
        # Dry run: no real order book to work with, so just show what the
        # market-order path would do (matches existing dry-run behavior).
        print(f"DRY RUN -> limit-then-market entry skipped, "
              f"would open {direction} at market")
        return open_position(symbol, direction, volume, live, sl=sl, magic_number=magic_number)

    symbol_info = ensure_symbol(symbol)
    tick = mt5.symbol_info_tick(symbol)

    if tick is None:
        print(f"ERROR: No market tick for {symbol}")
        return False

    volume = normalize_volume(symbol_info, volume)
    point = symbol_info.point
    digits = symbol_info.digits

    # Respect the broker's minimum pending-order distance, if any.
    stops_level_points = getattr(symbol_info, "trade_stops_level", 0) or 0
    distance_points = max(stops_level_points, offset_points)
    distance = distance_points * point

    if direction.upper() == "BUY":
        order_type = mt5.ORDER_TYPE_BUY_LIMIT
        limit_price = round(tick.ask - distance, digits)
    elif direction.upper() == "SELL":
        order_type = mt5.ORDER_TYPE_SELL_LIMIT
        limit_price = round(tick.bid + distance, digits)
    else:
        raise ValueError(direction)

    request = {
        "action": mt5.TRADE_ACTION_PENDING,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "price": limit_price,
        "magic": magic_number,
        "comment": "ChandraTrend-LIMIT",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": choose_filling_mode(symbol_info),
    }

    if sl is not None:
        request["sl"] = sl

    print(
        f"LIMIT ENTRY {direction}: symbol={symbol} volume={volume} "
        f"price={limit_price} (market ask/bid={tick.ask}/{tick.bid}, "
        f"waiting {wait_seconds}s before falling back to market)"
        + (f" sl={sl}" if sl is not None else "")
    )

    result = mt5.order_send(request)

    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        retcode = result.retcode if result else None
        comment = result.comment if result else mt5.last_error()
        print(
            f"LIMIT order rejected (retcode={retcode}, comment={comment}) "
            f"-> falling back to market order immediately"
        )
        return open_position(symbol, direction, volume, live, sl=sl, magic_number=magic_number)

    ticket = result.order

    # Poll until filled/removed, or until wait_seconds elapses.
    elapsed = 0.0
    check_interval = 0.5
    while elapsed < wait_seconds:
        time_module.sleep(check_interval)
        elapsed += check_interval
        if not mt5.orders_get(ticket=ticket):
            break

    still_pending = mt5.orders_get(ticket=ticket)

    if not still_pending:
        # No longer pending -- confirm it actually filled (vs. rejected/
        # expired for some other broker-side reason).
        history = mt5.history_orders_get(ticket=ticket)
        if history and history[0].state == mt5.ORDER_STATE_FILLED:
            print(f"LIMIT FILLED: ticket={ticket} price={limit_price}")
            return True
        print(
            f"LIMIT order ticket={ticket} left pending list without a "
            f"confirmed fill (state={history[0].state if history else 'unknown'}) "
            f"-> falling back to market order"
        )
        return open_position(symbol, direction, volume, live, sl=sl, magic_number=magic_number)

    # Still pending after the wait -- cancel it and use market instead.
    cancel_result = mt5.order_send({
        "action": mt5.TRADE_ACTION_REMOVE,
        "order": ticket,
    })
    if cancel_result is None or cancel_result.retcode != mt5.TRADE_RETCODE_DONE:
        print(
            f"WARNING: could not cancel unfilled limit order ticket={ticket} "
            f"(retcode={cancel_result.retcode if cancel_result else None}). "
            f"Check MT5 terminal manually -- skipping market fallback to "
            f"avoid double entry."
        )
        return False

    print(f"LIMIT order ticket={ticket} not filled in {wait_seconds}s, "
          f"cancelled -> sending market order instead")
    return open_position(symbol, direction, volume, live, sl=sl, magic_number=magic_number)


# ============================================================
# SIGNAL -> EXECUTION
# ============================================================

def _position_counts(symbol: str, state: ExecutionState, live: bool):
    """
    Live mode: read actual MT5 positions.
    Dry run: use the bot's virtual position so repeated signals do not
    repeatedly print OPEN orders.
    """
    if live:
        buys = get_buy_positions(symbol, state.magic_number)
        sells = get_sell_positions(symbol, state.magic_number)
        return buys, sells

    if state.virtual_position == 1:
        return ["DRYRUN_BUY"], []
    if state.virtual_position == -1:
        return [], ["DRYRUN_SELL"]
    return [], []


def _close_direction(
    symbol: str,
    direction: str,
    buys,
    sells,
    symbol_info,
    state: ExecutionState,
    live: bool,
) -> bool:
    if direction == "BUY":
        if live:
            for position in buys:
                if not close_position(position, symbol_info, live, state.magic_number):
                    return False
        else:
            if state.virtual_position == 1:
                print("DRY RUN -> CLOSE BUY (virtual position)")
            state.virtual_position = 0
        return True

    if direction == "SELL":
        if live:
            for position in sells:
                if not close_position(position, symbol_info, live, state.magic_number):
                    return False
        else:
            if state.virtual_position == -1:
                print("DRY RUN -> CLOSE SELL (virtual position)")
            state.virtual_position = 0
        return True

    return False


def _open_direction(
    symbol: str,
    direction: str,
    lot_size: float,
    state: ExecutionState,
    live: bool,
    l100: float | None = None,
) -> bool:
    if live:
        sl = None
        if STOPLOSS_CONFIG["enabled"] and l100 is not None:
            symbol_info = ensure_symbol(symbol)
            sl = compute_l100_stop_price(direction, l100, symbol_info)

        if ENTRY_CONFIG["mode"] == "limit":
            return open_position_limit_then_market(
                symbol=symbol,
                direction=direction,
                volume=lot_size,
                live=True,
                wait_seconds=ENTRY_CONFIG["limit_wait_seconds"],
                offset_points=ENTRY_CONFIG["limit_offset_points"],
                sl=sl,
                magic_number=state.magic_number,
            )
        return open_position(
            symbol=symbol,
            direction=direction,
            volume=lot_size,
            live=True,
            sl=sl,
            magic_number=state.magic_number,
        )

    print(f"DRY RUN -> OPEN {direction} (virtual position)")
    state.virtual_position = 1 if direction == "BUY" else -1
    return True


def _enter(
    symbol: str,
    direction: str,
    lot_size: float,
    live: bool,
    state: ExecutionState,
    l100: float | None = None,
):
    symbol_info = ensure_symbol(symbol)
    buys, sells = _position_counts(symbol, state, live)

    if direction == "BUY":
        if buys:
            print("BUY entry signal + BUY already open -> HOLD BUY")
            return

        if sells:
            print("BUY entry signal + SELL open -> CLOSE SELL -> OPEN BUY")
            if not _close_direction(
                symbol, "SELL", buys, sells, symbol_info, state, live
            ):
                print("Could not close SELL. BUY NOT opened.")
                return

        if live and get_sell_positions(symbol, state.magic_number):
            print("SELL position still exists. BUY NOT opened.")
            return

        if _open_direction(symbol, "BUY", lot_size, state, live, l100=l100):
            print("BUY ENTRY COMPLETE")

    else:
        if sells:
            print("SELL entry signal + SELL already open -> HOLD SELL")
            return

        if buys:
            print("SELL entry signal + BUY open -> CLOSE BUY -> OPEN SELL")
            if not _close_direction(
                symbol, "BUY", buys, sells, symbol_info, state, live
            ):
                print("Could not close BUY. SELL NOT opened.")
                return

        if live and get_buy_positions(symbol, state.magic_number):
            print("BUY position still exists. SELL NOT opened.")
            return

        if _open_direction(symbol, "SELL", lot_size, state, live, l100=l100):
            print("SELL ENTRY COMPLETE")


def _exit_buy_if_triggered(
    symbol: str,
    close: float,
    signal: SignalState,
    lot_size: float,
    live: bool,
    state: ExecutionState,
):
    """
    BUY exit:
      buy_sell mode -> SellCondition candle LOW, then later CLOSE < LOW
      magical mode  -> Magical BUY candle LOW, then later CLOSE < LOW
    """
    if not np.isnan(state.buy_exit_low) and close < state.buy_exit_low:
        print(
            f"BUY EXIT TRIGGERED: close={close:.5f} "
            f"< reference LOW={state.buy_exit_low:.5f}"
        )
        buys, sells = _position_counts(symbol, state, live)
        symbol_info = ensure_symbol(symbol)

        if buys:
            ok = _close_direction(
                symbol, "BUY", buys, sells, symbol_info, state, live
            )
            if ok:
                print("BUY EXIT COMPLETE")
                state.buy_exit_low = np.nan
        return True

    return False


def _exit_sell_if_triggered(
    symbol: str,
    close: float,
    signal: SignalState,
    lot_size: float,
    live: bool,
    state: ExecutionState,
):
    """
    SELL exit:
      buy_sell mode -> BuyCondition candle HIGH, then later CLOSE > HIGH
      magical mode  -> Magical SELL candle HIGH, then later CLOSE > HIGH
    """
    if not np.isnan(state.sell_exit_high) and close > state.sell_exit_high:
        print(
            f"SELL EXIT TRIGGERED: close={close:.5f} "
            f"> reference HIGH={state.sell_exit_high:.5f}"
        )
        buys, sells = _position_counts(symbol, state, live)
        symbol_info = ensure_symbol(symbol)

        if sells:
            ok = _close_direction(
                symbol, "SELL", buys, sells, symbol_info, state, live
            )
            if ok:
                print("SELL EXIT COMPLETE")
                state.sell_exit_high = np.nan
        return True

    return False


def _new_event(current: bool, previous: bool) -> bool:
    return bool(current) and not bool(previous)


def execute_strategy(
    symbol: str,
    signal: SignalState,
    lot_size: float,
    live: bool,
    state: ExecutionState,
):
    """
    Execute only the selected signal mode.

    Startup:
      The first calculated candle establishes a baseline and never enters.

    buy_sell mode:
      BUY  = BuyCondition followed by Strategic BUY.
      SELL = SellCondition followed by Strategic SELL.

      BUY exit  = SellCondition TRUE -> close BUY immediately.
      SELL exit = BuyCondition TRUE -> close SELL immediately.

    magical mode:
      BUY  = new Magical BUY event.
      SELL = new Magical SELL event.

      BUY exit  = Magical BUY signal candle LOW, then later close below LOW.
      SELL exit = Magical SELL signal candle HIGH, then later close above HIGH.
    """
    buy_condition = bool(signal.buy_condition)
    sell_condition = bool(signal.sell_condition)
    strategic_buy = bool(signal.strategic_buy)
    strategic_sell = bool(signal.strategic_sell)
    magical_buy = bool(signal.magical_buy)
    magical_sell = bool(signal.magical_sell)

    new_buy_condition = _new_event(buy_condition, state.prev_buy_condition)
    new_sell_condition = _new_event(sell_condition, state.prev_sell_condition)
    new_strategic_buy = _new_event(strategic_buy, state.prev_strategic_buy)
    new_strategic_sell = _new_event(strategic_sell, state.prev_strategic_sell)
    new_magical_buy = _new_event(magical_buy, state.prev_magical_buy)
    new_magical_sell = _new_event(magical_sell, state.prev_magical_sell)

    buys, sells = _position_counts(symbol, state, live)

    # Only print the detailed execution block when one of the trading
    # signals actually changes. This keeps the continuous monitor quiet
    # while a signal remains unchanged candle after candle.
    signal_signature = (
        buy_condition, sell_condition,
        strategic_buy, strategic_sell,
        magical_buy, magical_sell,
    )
    signal_changed = state.last_log_signature != signal_signature
    if signal_changed:
        state.last_log_signature = signal_signature
        print()
        print("-" * 72)
        print(f"EXECUTION: {symbol}")
        print(f"Trading mode        : {state.mode.upper()}")
        print(f"Pine BUY condition  : {buy_condition}")
        print(f"Pine SELL condition : {sell_condition}")
        print(f"Strategic BUY       : {strategic_buy}")
        print(f"Strategic SELL      : {strategic_sell}")
        print(f"Magical BUY         : {magical_buy}")
        print(f"Magical SELL        : {magical_sell}")
        print(f"MT5 BUY positions   : {len(buys)}")
        print(f"MT5 SELL positions  : {len(sells)}")
        print(
            f"BUY exit LOW        : "
            f"{state.buy_exit_low:.5f}" if not np.isnan(state.buy_exit_low)
            else "BUY exit LOW        : NONE"
        )
        print(
            f"SELL exit HIGH      : "
            f"{state.sell_exit_high:.5f}" if not np.isnan(state.sell_exit_high)
            else "SELL exit HIGH      : NONE"
        )
        print("-" * 72)

    # --------------------------------------------------------
    # STARTUP BASELINE
    # --------------------------------------------------------
    if not state.initialized:
        state.initialized = True

        # Establish the current signal as the baseline.
        # IMPORTANT: a base BUY/SELL condition that is already active at
        # startup must NOT place an order immediately, but it should remain
        # ARMED so that a later Strategic BUY/SELL confirmation can trigger
        # the entry. This matches the intended:
        #
        #   SellCondition TRUE -> wait -> Strategic SELL TRUE -> SELL
        #   BuyCondition  TRUE -> wait -> Strategic BUY  TRUE -> BUY
        #
        # If the base condition disappears before the strategic confirmation,
        # execute_strategy() will cancel the corresponding armed state.
        state.buy_armed = buy_condition if state.mode == "buy_sell" else False
        state.sell_armed = sell_condition if state.mode == "buy_sell" else False

        state.prev_buy_condition = buy_condition
        state.prev_sell_condition = sell_condition
        state.prev_strategic_buy = strategic_buy
        state.prev_strategic_sell = strategic_sell
        state.prev_magical_buy = magical_buy
        state.prev_magical_sell = magical_sell

        print("STARTUP BASELINE -> NO ORDER")
        if state.mode == "buy_sell":
            print("Strategic Entry startup baseline -> waiting for the next confirmed retest entry.")
        else:
            print("Current signals are stored. Waiting for next Magical signal.")
        print("-" * 72)
        return

    # --------------------------------------------------------
    # ENTRY
    # --------------------------------------------------------
    if not state.entries_enabled:
        state.prev_buy_condition = buy_condition
        state.prev_sell_condition = sell_condition
        state.prev_strategic_buy = strategic_buy
        state.prev_strategic_sell = strategic_sell
        state.prev_magical_buy = magical_buy
        state.prev_magical_sell = magical_sell
        if live and STOPLOSS_CONFIG["enabled"] and state.mode != "buy_sell":
            sync_l100_stoploss(symbol, signal.l100, state.magic_number)
        return

    if state.mode == "buy_sell":
        # Strategic Entry uses the confirmed retest event.
        # No BuyCondition/SellCondition arming and no L100 price gate.
        # The broker-side protective SL remains separate from the entry rule.
        buys_now, sells_now = _position_counts(symbol, state, live)

        if new_strategic_buy and not buys_now and not sells_now:
            print("STRATEGIC BUY ENTRY SIGNAL: retest confirmed")
            _enter(symbol, "BUY", lot_size, live, state, l100=signal.l100)

        elif new_strategic_sell and not buys_now and not sells_now:
            print("STRATEGIC SELL ENTRY SIGNAL: retest confirmed")
            _enter(symbol, "SELL", lot_size, live, state, l100=signal.l100)

        elif new_strategic_buy and (buys_now or sells_now):
            print("Strategic BUY signal while position is open -> WAIT for exit rule")

        elif new_strategic_sell and (buys_now or sells_now):
            print("Strategic SELL signal while position is open -> WAIT for exit rule")

        elif signal_changed:
            print("No NEW Strategic entry confirmation -> WAIT")

    else:
        # Magical mode remains unchanged.
        if new_magical_buy and not signal.magical_invalid:
            print("MAGICAL BUY ENTRY SIGNAL")
            _enter(symbol, "BUY", lot_size, live, state, l100=signal.l100)

        elif new_magical_sell and not signal.magical_invalid:
            print("MAGICAL SELL ENTRY SIGNAL")
            _enter(symbol, "SELL", lot_size, live, state, l100=signal.l100)

        elif signal_changed:
            print("No NEW Magical BUY/SELL signal -> WAIT")

    # Update previous event state after processing this candle.
    state.prev_buy_condition = buy_condition
    state.prev_sell_condition = sell_condition
    state.prev_strategic_buy = strategic_buy
    state.prev_strategic_sell = strategic_sell
    state.prev_magical_buy = magical_buy
    state.prev_magical_sell = magical_sell

    # Strategic mode keeps the initial L100 +/- 5 protection fixed after entry.
    # Magical mode retains its existing L100 sync behavior.
    if live and STOPLOSS_CONFIG["enabled"] and state.mode != "buy_sell":
        sync_l100_stoploss(symbol, signal.l100, state.magic_number)


def process_candle(
    symbol: str,
    row,
    signal: SignalState,
    lot_size: float,
    live: bool,
    state: ExecutionState,
):
    """
    Process one closed candle.

    This function owns the OHLC-dependent exit references because the
    SignalState intentionally contains indicator/signal values only.
    """
    close = float(row["close"])
    low = float(row["low"])
    high = float(row["high"])

    # Track whether this candle itself created a dynamic exit reference.
    state.buy_reference_set_this_candle = False
    state.sell_reference_set_this_candle = False

    # Determine current position before processing exits.
    buys, sells = _position_counts(symbol, state, live)

    # For event detection we need the previous values before execute_strategy.
    new_sell_condition = _new_event(
        signal.sell_condition, state.prev_sell_condition
    )
    new_buy_condition = _new_event(
        signal.buy_condition, state.prev_buy_condition
    )
    new_magical_buy = _new_event(
        signal.magical_buy, state.prev_magical_buy
    )
    new_magical_sell = _new_event(
        signal.magical_sell, state.prev_magical_sell
    )

    # FINAL EXIT RULES
    # strategic mode:
    #   BUY  -> initial SL is L100-5. When a NEW SELL signal candle appears,
    #           remember that candle LOW. A LATER candle must CLOSE below it
    #           to exit the BUY.
    #   SELL -> initial SL is L100+5. When a NEW BUY signal candle appears,
    #           remember that candle HIGH. A LATER candle must CLOSE above it
    #           to exit the SELL.
    # magical mode keeps its existing reference-candle exits.
    if state.mode == "buy_sell":
        # The reference is deliberately captured on the opposite signal
        # candle, but the exit check happens on subsequent candles only.
        if buys and new_sell_condition:
            state.buy_exit_low = low
            state.buy_reference_set_this_candle = True
            print(
                f"BUY EXIT REFERENCE UPDATED: SELL signal candle LOW = "
                f"{state.buy_exit_low:.5f}"
            )

        if sells and new_buy_condition:
            state.sell_exit_high = high
            state.sell_reference_set_this_candle = True
            print(
                f"SELL EXIT REFERENCE UPDATED: BUY signal candle HIGH = "
                f"{state.sell_exit_high:.5f}"
            )

        # Do not use broker-side SL for the dynamic signal reference.
        # The strategy requires candle-CLOSE confirmation, not an intrabar
        # price touch. The initial L100 +/- 5 protection remains broker-side.
        buys, sells = _position_counts(symbol, state, live)

        if buys and not np.isnan(state.buy_exit_low):
            # Skip the candle that created the reference; the next processed
            # closed candle is the first candle eligible to trigger the exit.
            if not getattr(state, "buy_reference_set_this_candle", False):
                if close < state.buy_exit_low:
                    print(
                        f"BUY EXIT TRIGGERED: close={close:.5f} < "
                        f"SELL signal LOW={state.buy_exit_low:.5f}"
                    )
                    symbol_info = ensure_symbol(symbol)
                    if _close_direction(
                        symbol, "BUY", buys, sells, symbol_info, state, live
                    ):
                        state.buy_exit_low = np.nan

        if sells and not np.isnan(state.sell_exit_high):
            if not getattr(state, "sell_reference_set_this_candle", False):
                if close > state.sell_exit_high:
                    print(
                        f"SELL EXIT TRIGGERED: close={close:.5f} > "
                        f"BUY signal HIGH={state.sell_exit_high:.5f}"
                    )
                    symbol_info = ensure_symbol(symbol)
                    if _close_direction(
                        symbol, "SELL", buys, sells, symbol_info, state, live
                    ):
                        state.sell_exit_high = np.nan

    else:
        # Magical mode: signal candle becomes the exit reference.
        if new_magical_buy:
            state.buy_exit_low = low
            print(
                f"MAGICAL BUY EXIT REFERENCE: candle LOW = "
                f"{state.buy_exit_low:.5f}"
            )

        if new_magical_sell:
            state.sell_exit_high = high
            print(
                f"MAGICAL SELL EXIT REFERENCE: candle HIGH = "
                f"{state.sell_exit_high:.5f}"
            )

        buys, sells = _position_counts(symbol, state, live)

        if buys and not np.isnan(state.buy_exit_low) and close < state.buy_exit_low:
            print(
                f"BUY EXIT: candle CLOSE {close:.5f} < "
                f"MAGICAL BUY LOW {state.buy_exit_low:.5f}"
            )
            symbol_info = ensure_symbol(symbol)
            if _close_direction(
                symbol, "BUY", buys, sells, symbol_info, state, live
            ):
                state.buy_exit_low = np.nan

        buys, sells = _position_counts(symbol, state, live)

        if sells and not np.isnan(state.sell_exit_high) and close > state.sell_exit_high:
            print(
                f"SELL EXIT: candle CLOSE {close:.5f} > "
                f"MAGICAL SELL HIGH {state.sell_exit_high:.5f}"
            )
            symbol_info = ensure_symbol(symbol)
            if _close_direction(
                symbol, "SELL", buys, sells, symbol_info, state, live
            ):
                state.sell_exit_high = np.nan

    # After exits, process entries/reversals.
    execute_strategy(
        symbol=symbol,
        signal=signal,
        lot_size=lot_size,
        live=live,
        state=state,
    )

    # Clear stale Magical exit references when the corresponding position is gone.
    buys, sells = _position_counts(symbol, state, live)
    if not buys:
        state.buy_exit_low = np.nan
    if not sells:
        state.sell_exit_high = np.nan

# ============================================================
# DISPLAY
# ============================================================

def print_status(df: pd.DataFrame, symbol: str, tf: str):
    row = df.iloc[-1]

    signal = (
        "BUY CONDITION"
        if bool(row["buyCondition"]) and not bool(row["sellCondition"])
        else "SELL CONDITION"
        if bool(row["sellCondition"]) and not bool(row["buyCondition"])
        else "AMBIGUOUS"
        if bool(row["buyCondition"]) and bool(row["sellCondition"])
        else "WAIT"
    )

    print()
    print("=" * 72)
    print("CHANDRA TREND ENGINE - MT5")
    print("=" * 72)
    print(f"Symbol       : {symbol}")
    print(f"Timeframe    : {tf}")
    print(f"Candle       : {row['time']}")
    print(f"Open         : {row['open']}")
    print(f"High         : {row['high']}")
    print(f"Low          : {row['low']}")
    print(f"Close        : {row['close']}")
    print("-" * 72)
    print(f"L100         : {row['l100']:.5f}" if pd.notna(row["l100"]) else "L100         : NA")
    print(f"RSI(14)      : {row['rsi']:.2f}" if pd.notna(row["rsi"]) else "RSI(14)      : NA")
    print(f"EMA(1000)    : {row['emaValue']:.5f}" if pd.notna(row["emaValue"]) else "EMA(1000)    : NA")
    print(f"Trend        : {int(row['trend'])}")
    print("-" * 72)
    print(f"BuyCondition : {bool(row['buyCondition'])}")
    print(f"SellCondition: {bool(row['sellCondition'])}")
    print(f"NEW BUY      : {bool(row['newBuySignal'])}")
    print(f"NEW SELL     : {bool(row['newSellSignal'])}")
    print(f"Position     : {'BUY' if row['position'] == 1 else 'SELL' if row['position'] == -1 else 'NONE'}")
    print("-" * 72)
    print(f"Sunghamam    : {bool(row['sunghamam'])}")
    print(f"Maha         : {bool(row['mahaSunghamam'])}")
    print(f"G-BUY        : {bool(row['gBuyCondition'])}")
    print(f"G-SELL       : {bool(row['gSellCondition'])}")
    print(f"Fake BUY     : {bool(row['fakeBuyCondition'])}")
    print(f"Fake SELL    : {bool(row['fakeSellCondition'])}")
    print(f"Strategic BUY: {bool(row['strategicBuy'])}")
    print(f"Strategic SELL:{bool(row['strategicSell'])}")
    print(f"Magical BUY  : {bool(row['magicalBuy'])}")
    print(f"Magical SELL : {bool(row['magicalSell'])}")
    print("-" * 72)
    print(f"ACTION       : {signal}")
    print("MT5 positions are checked separately before every order decision.")
    print("=" * 72)


# ============================================================
# ONCE-PER-BAR-CLOSE LOOP
# ============================================================

def signal_from_row(row) -> SignalState:
    """Build a SignalState from the latest calculated Pine row."""
    return SignalState(
        position=int(row["position"]),
        buy_condition=bool(row["buyCondition"]),
        sell_condition=bool(row["sellCondition"]),
        new_buy_signal=bool(row["newBuySignal"]),
        new_sell_signal=bool(row["newSellSignal"]),
        close=float(row["close"]),
        norm_close=float(row["normClose"]),
        supertrend=float(row["supertrend"]),
        l100=float(row["l100"]),
        rsi=float(row["rsi"]),
        ema=float(row["emaValue"]),
        trend=int(row["trend"]),
        extreme_point=float(row["extremePoint"]),
        fib3=float(row["fib3"]),
        volume_line=float(row["volumeLine"]),
        sunghamam=bool(row["sunghamam"]),
        maha_sunghamam=bool(row["mahaSunghamam"]),
        g_buy=bool(row["gBuyCondition"]),
        g_sell=bool(row["gSellCondition"]),
        fake_buy=bool(row["fakeBuyCondition"]),
        fake_sell=bool(row["fakeSellCondition"]),
        strategic_buy=bool(row["strategicBuy"]),
        strategic_sell=bool(row["strategicSell"]),
        magical_buy=bool(row["magicalBuy"]),
        magical_sell=bool(row["magicalSell"]),
        magical_invalid=bool(row["magicalInvalid"]),
    )

def should_print_status(state: ExecutionState, row) -> bool:
    """Return True only when a displayed trading signal changes."""
    signature = (
        bool(row["buyCondition"]),
        bool(row["sellCondition"]),
        bool(row["strategicBuy"]),
        bool(row["strategicSell"]),
        bool(row["magicalBuy"]),
        bool(row["magicalSell"]),
    )
    if state.last_status_signature == signature:
        return False
    state.last_status_signature = signature
    return True


def run_once(
    symbol: str,
    tf: str,
    bars: int,
    lot_size: float,
    live: bool,
    signal_mode: str,
):
    ensure_symbol(symbol)

    df = get_rates(symbol, tf, bars)

    if len(df) < EMA_PERIOD + ATR_PERIOD + 100:
        raise RuntimeError(
            f"Not enough closed candles. Got {len(df)}, "
            f"need at least {EMA_PERIOD + ATR_PERIOD + 100}."
        )

    result = calculate_pine_engine(df)
    if should_print_status(state, result.iloc[-1]):
        print_status(result, symbol, tf)

    signal = signal_from_row(result.iloc[-1])

    state = ExecutionState(mode=signal_mode)

    # In --once mode, this is intentionally a startup baseline. It never
    # places an order from the already-active signal.
    process_candle(
        symbol=symbol,
        row=result.iloc[-1],
        signal=signal,
        lot_size=lot_size,
        live=live,
        state=state,
    )

    return result


def run_live_loop(
    symbol: str,
    tf: str,
    bars: int,
    lot_size: float,
    live: bool,
    signal_mode: str,
):
    """
    Continuously monitor MT5 and evaluate the strategy on each newly CLOSED
    candle.

    The first candle establishes the startup baseline and does not enter.
    """
    minutes = timeframe_minutes(tf)
    poll_seconds = max(2, min(5, minutes))

    state = ExecutionState(mode=signal_mode)

    print()
    print("RUNNING CONTINUOUS MT5 MONITOR")
    print(f"Symbol     : {symbol}")
    print(f"Timeframe  : {tf}")
    print(f"Mode       : {'LIVE' if live else 'DRY RUN'}")
    print(f"Lot size   : {lot_size}")
    print(f"Signal mode: {signal_mode}")
    print(f"Polling    : {poll_seconds} seconds")
    print()
    print("Press CTRL+C to stop.")
    print()

    last_closed_candle = None

    while True:
        try:
            df = get_rates(symbol, tf, bars)

            if len(df) < EMA_PERIOD + ATR_PERIOD + 100:
                print("Waiting for enough historical data...")
                time_module.sleep(5)
                continue

            closed_time = df.iloc[-1]["time"]

            if last_closed_candle is None or closed_time > last_closed_candle:
                is_first = last_closed_candle is None
                last_closed_candle = closed_time

                result = calculate_pine_engine(df)
                row = result.iloc[-1]

                # Only print candle/status output when a tracked signal
                # changes. Do not print a repeated candle header every bar.
                status_changed = should_print_status(state, row)
                if status_changed:
                    print()
                    if is_first:
                        print(
                            f"INITIAL CLOSED CANDLE: "
                            f"{symbol} {tf} @ {closed_time}"
                        )
                    else:
                        print(
                            f"SIGNAL CHANGE: "
                            f"{symbol} {tf} @ {closed_time}"
                        )
                    print_status(result, symbol, tf)

                signal = signal_from_row(row)

                process_candle(
                    symbol=symbol,
                    row=row,
                    signal=signal,
                    lot_size=lot_size,
                    live=live,
                    state=state,
                )

            time_module.sleep(poll_seconds)

        except KeyboardInterrupt:
            print("\nStopped by user.")
            break

        except Exception as exc:
            print(f"ERROR in loop: {exc}")
            time_module.sleep(5)


# ============================================================
# MAIN
# ============================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description="Chandra Trend Engine Pine -> MT5"
    )

    parser.add_argument(
        "--symbol",
        default="XAUUSD",
        help="MT5 symbol, e.g. XAUUSD or XAUUSDm",
    )

    parser.add_argument(
        "--tf",
        default="15",
        help="Timeframe: 1,3,5,15,30,60,H1,H4,D",
    )

    parser.add_argument(
        "--bars",
        type=int,
        default=3000,
        help="Historical closed candles",
    )

    parser.add_argument(
        "--lot",
        type=float,
        default=0.01,
        help="Trading lot size",
    )

    parser.add_argument(
        "--signal-mode",
        choices=["buy_sell", "magical"],
        default="buy_sell",
        help=(
            "Trading mode: buy_sell = BuyCondition+Strategic confirmation; "
            "magical = Magical BUY/SELL"
        ),
    )

    parser.add_argument(
        "--live",
        action="store_true",
        help="ENABLE REAL MT5 ORDERS. Without this flag = DRY RUN.",
    )

    parser.add_argument(
        "--test-order",
        choices=["buy", "sell"],
        default=None,
        help=(
            "Bypass all signal logic and immediately send ONE market order "
            "in the given direction, then exit. Useful for confirming the "
            "live order path (connection, symbol, filling mode, broker "
            "permissions) works end to end. Combine with --live to actually "
            "send it; without --live it just prints what would be sent."
        ),
    )

    parser.add_argument(
        "--entry-mode",
        choices=["market", "limit"],
        default="market",
        help=(
            "How real strategy-triggered entries are placed (exits always "
            "use market orders). 'market' (default) sends a market order "
            "immediately. 'limit' places a limit order priced slightly "
            "better than the current market, waits --limit-wait-seconds "
            "for it to fill, and falls back to a market order if it "
            "doesn't fill in time."
        ),
    )

    parser.add_argument(
        "--limit-wait-seconds",
        type=float,
        default=2.5,
        help="Seconds to wait for a limit entry to fill before falling "
             "back to a market order (only used with --entry-mode limit).",
    )

    parser.add_argument(
        "--limit-offset-points",
        type=float,
        default=5,
        help="How many points better than market to price the limit entry "
             "(only used with --entry-mode limit). The broker's own "
             "minimum pending-order distance is respected automatically.",
    )

    parser.add_argument(
        "--sl-offset-points",
        type=float,
        default=None,
        help=(
            "Enable an L100-based stop loss and set it this many points "
            "away from the current L100 (supertrend) value. BUY SL = "
            "L100 - offset. SELL SL = L100 + offset. Set at entry, then "
            "automatically re-modified on the broker every candle as L100 "
            "moves, so the stop trails the trend. Omit this flag to leave "
            "positions without any broker-side stop loss (default)."
        ),
    )

    parser.add_argument(
        "--once",
        action="store_true",
        help=(
            "Run once and exit. Startup baseline is used, so no order is "
            "triggered from the current signal."
        ),
    )

    return parser.parse_args()


def main():
    args = parse_args()

    print()
    print("=" * 72)
    print("CHANDRA TREND ENGINE -> MT5")
    print("=" * 72)
    print(f"Symbol       : {args.symbol}")
    print(f"Timeframe    : {args.tf}")
    print(f"Bars         : {args.bars}")
    print(f"Lot          : {args.lot}")
    print(f"Signal mode  : {args.signal_mode}")
    print(f"Mode         : {'*** LIVE TRADING ***' if args.live else 'DRY RUN'}")
    print("=" * 72)

    if args.live:
        print()
        print("WARNING: LIVE MODE IS ENABLED.")
        print("Real orders may be sent to the connected MT5 account.")
        print()

    ENTRY_CONFIG["mode"] = args.entry_mode
    ENTRY_CONFIG["limit_wait_seconds"] = args.limit_wait_seconds
    ENTRY_CONFIG["limit_offset_points"] = args.limit_offset_points

    if args.entry_mode == "limit":
        print(f"Entry mode   : LIMIT (wait {args.limit_wait_seconds}s, "
              f"offset {args.limit_offset_points} points, "
              f"then fall back to market)")
        print(f"Exit mode    : MARKET (always)")
        print("=" * 72)

    if args.sl_offset_points is not None:
        STOPLOSS_CONFIG["enabled"] = True
        STOPLOSS_CONFIG["offset_points"] = args.sl_offset_points
        print(f"Stop loss    : L100-based, offset {args.sl_offset_points} "
              f"points (BUY=L100-offset, SELL=L100+offset), "
              f"re-synced every candle")
        print("=" * 72)

    connect_mt5()

    try:
        ensure_symbol(args.symbol)

        if args.test_order:
            print()
            print("=" * 72)
            print(f"TEST ORDER MODE: sending a single {args.test_order.upper()} "
                  f"order, bypassing all signal logic.")
            print("=" * 72)
            ok = open_position(
                symbol=args.symbol,
                direction=args.test_order,
                volume=args.lot,
                live=args.live,
            )
            print("TEST ORDER RESULT:", "OK" if ok else "FAILED")
            return

        if args.once:
            run_once(
                symbol=args.symbol,
                tf=args.tf,
                bars=args.bars,
                lot_size=args.lot,
                live=args.live,
                signal_mode=args.signal_mode,
            )
        else:
            run_live_loop(
                symbol=args.symbol,
                tf=args.tf,
                bars=args.bars,
                lot_size=args.lot,
                live=args.live,
                signal_mode=args.signal_mode,
            )

    finally:
        mt5.shutdown()
        print("MT5 connection closed.")


if __name__ == "__main__":
    main()