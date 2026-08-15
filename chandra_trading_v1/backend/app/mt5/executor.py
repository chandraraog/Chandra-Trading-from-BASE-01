from dataclasses import dataclass
from typing import Any
from backend.app.core.config import settings

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None

@dataclass
class MT5Result:
    ok: bool
    message: str
    data: Any = None

class MT5Executor:
    def initialize(self) -> MT5Result:
        if mt5 is None:
            return MT5Result(False, "MetaTrader5 package is not installed")
        kwargs = {}
        if settings.mt5_path:
            kwargs["path"] = settings.mt5_path
        ok = mt5.initialize(**kwargs)
        if not ok:
            return MT5Result(False, f"MT5 initialize failed: {mt5.last_error()}")
        return MT5Result(True, "MT5 initialized")

    def shutdown(self):
        if mt5 is not None:
            mt5.shutdown()

    def account_info(self):
        if mt5 is None:
            return None
        return mt5.account_info()

    def positions(self, symbol=None):
        if mt5 is None:
            return []
        result = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        return list(result or [])

    def symbol_info_tick(self, symbol):
        if mt5 is None:
            return None
        if not mt5.symbol_select(symbol, True):
            return None
        return mt5.symbol_info_tick(symbol)

    def market_order(self, symbol: str, side: str, volume: float, live: bool = False):
        if not live:
            return MT5Result(True, "DRY RUN: order not sent", {"symbol": symbol, "side": side, "volume": volume})
        if mt5 is None:
            return MT5Result(False, "MetaTrader5 package unavailable")
        init = self.initialize()
        if not init.ok:
            return init

        tick = self.symbol_info_tick(symbol)
        info = mt5.symbol_info(symbol)
        if tick is None or info is None:
            return MT5Result(False, f"Symbol unavailable: {symbol}")

        order_type = mt5.ORDER_TYPE_BUY if side.upper() == "BUY" else mt5.ORDER_TYPE_SELL
        price = tick.ask if side.upper() == "BUY" else tick.bid

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "price": price,
            "deviation": 20,
            "magic": 26081301,
            "comment": "CHANDRA_TREND_V1",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": getattr(info, "filling_mode", mt5.ORDER_FILLING_IOC),
        }
        result = mt5.order_send(request)
        if result is None:
            return MT5Result(False, f"order_send returned None: {mt5.last_error()}")
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return MT5Result(False, f"Broker rejected order: {result.retcode}", result)
        return MT5Result(True, "ORDER SENT", result)

    def close_position(self, position, live: bool = False):
        if not live:
            return MT5Result(True, "DRY RUN: close not sent")
        if mt5 is None:
            return MT5Result(False, "MetaTrader5 package unavailable")
        tick = self.symbol_info_tick(position.symbol)
        if tick is None:
            return MT5Result(False, "No tick")
        side = "SELL" if position.type == mt5.POSITION_TYPE_BUY else "BUY"
        order_type = mt5.ORDER_TYPE_SELL if side == "SELL" else mt5.ORDER_TYPE_BUY
        price = tick.bid if side == "SELL" else tick.ask
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": position.volume,
            "type": order_type,
            "position": position.ticket,
            "price": price,
            "deviation": 20,
            "magic": 26081301,
            "comment": "CHANDRA_TREND_V1_EXIT",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None:
            return MT5Result(False, f"close failed: {mt5.last_error()}")
        return MT5Result(result.retcode == mt5.TRADE_RETCODE_DONE, str(result.retcode), result)
