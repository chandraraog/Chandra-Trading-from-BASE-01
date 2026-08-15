from dataclasses import dataclass
from typing import Optional
from . import chandra_core as core

@dataclass
class EngineSignal:
    buy_condition: bool = False
    sell_condition: bool = False
    strategic_buy: bool = False
    strategic_sell: bool = False
    magical_buy: bool = False
    magical_sell: bool = False
    magical_invalid: bool = False
    close: Optional[float] = None
    time: Optional[str] = None

class ChandraTrendEngine:
    """Web adapter around the exact supplied Chandra Trend Engine v5."""
    def __init__(self, symbol="XAUUSD", timeframe="M1", bars=3000):
        self.symbol, self.timeframe, self.bars = symbol, timeframe, bars

    def initialize(self):
        core.connect_mt5()
        core.ensure_symbol(self.symbol)

    def shutdown(self):
        try:
            core.mt5.shutdown()
        except Exception:
            pass

    def calculate_latest(self) -> EngineSignal:
        df = core.get_rates(self.symbol, self.timeframe, self.bars)
        df = core.calculate_pine_engine(df)
        row = df.iloc[-1]
        sig = core.signal_from_row(row)
        return EngineSignal(
            buy_condition=sig.buy_condition,
            sell_condition=sig.sell_condition,
            strategic_buy=sig.strategic_buy,
            strategic_sell=sig.strategic_sell,
            magical_buy=sig.magical_buy,
            magical_sell=sig.magical_sell,
            magical_invalid=sig.magical_invalid,
            close=sig.close,
            time=str(row["time"]),
        )

    def calculate_frame(self):
        df = core.get_rates(self.symbol, self.timeframe, self.bars)
        return core.calculate_pine_engine(df)

    def selected_event(self, signal: EngineSignal, strategy: str):
        if strategy == "buy_sell":
            return {"buy": signal.buy_condition and signal.strategic_buy,
                    "sell": signal.sell_condition and signal.strategic_sell}
        if strategy == "magical":
            return {"buy": signal.magical_buy and not signal.magical_invalid,
                    "sell": signal.magical_sell and not signal.magical_invalid}
        raise ValueError("strategy must be buy_sell or magical")
