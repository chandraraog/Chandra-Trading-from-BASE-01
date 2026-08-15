from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from backend.app.api.deps import current_user
from backend.app.db.database import get_db
from backend.app.db.models import BotConfig, User
from backend.app.services.bot_manager import manager

router = APIRouter(prefix="/api/trading", tags=["trading"])

class ConfigIn(BaseModel):
    strategies: list[str] = Field(min_length=1)
    symbol: str = "XAUUSD"
    timeframe: str = "M1"
    lot: float = Field(default=0.01, gt=0, le=100)
    mode: str = Field(default="paper", pattern="^(paper|live)$")
    timing_hour: int = Field(default=9, ge=0, le=23)
    timing_minute: int = Field(default=30, ge=0, le=59)

@router.get("/status")
def status(user: User = Depends(current_user)):
    return manager.status_payload()

@router.get("/trades")
def trades(user: User = Depends(current_user)):
    return {"trades": manager.state.trades}

@router.post("/mt5-test")
def mt5_test(user: User = Depends(current_user)):
    return manager.mt5_test()

@router.get("/market")
def market(user: User = Depends(current_user)):
    result = manager.mt5_test()
    return result

@router.post("/configure")
def configure(payload: ConfigIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    cfg = db.query(BotConfig).filter(BotConfig.user_id == user.id).first()
    if not cfg:
        cfg = BotConfig(user_id=user.id); db.add(cfg)
    if any(s not in ("strategic", "magical", "timing") for s in payload.strategies):
        raise ValueError("Invalid strategy")
    cfg.strategy = ",".join(payload.strategies); cfg.symbol = payload.symbol; cfg.timeframe = payload.timeframe
    cfg.lot = payload.lot; cfg.live_enabled = payload.mode == "live"
    db.commit()
    manager.configure(payload.strategies, payload.symbol, payload.timeframe, payload.lot, payload.mode, payload.timing_hour, payload.timing_minute)
    return {"ok": True, "mode": payload.mode, "strategies": payload.strategies}

@router.post("/start")
def start(user: User = Depends(current_user)):
    manager.start()
    return {"ok": True, "running": True}

@router.post("/stop")
def stop(user: User = Depends(current_user)):
    manager.stop()
    return {"ok": True, "running": False}

@router.post("/stop-trading")
def stop_trading(user: User = Depends(current_user)):
    manager.stop_trading()
    return {"ok": True, "entries_enabled": False, "message": "New entries disabled; existing positions remain managed."}

@router.post("/resume-trading")
def resume_trading(user: User = Depends(current_user)):
    manager.resume_trading()
    return {"ok": True, "entries_enabled": True}

@router.post("/close/{strategy}")
def close_strategy(strategy: str, user: User = Depends(current_user)):
    if strategy not in ("strategic", "magical", "timing"):
        raise ValueError("Invalid strategy")
    return manager.close_strategy(strategy)

@router.post("/close-all")
def close_all(user: User = Depends(current_user)):
    return manager.close_all()

class ManualOrderIn(BaseModel):
    side: str = Field(pattern="^(BUY|SELL)$")
    lot: float = Field(default=0.01, gt=0, le=100)
    sl: float | None = Field(default=None, gt=0)
    target: float | None = Field(default=None, gt=0)

@router.post("/manual/order")
def manual_order(payload: ManualOrderIn, user: User = Depends(current_user)):
    return manager.manual_order(payload.side, payload.lot, payload.sl, payload.target)

@router.post("/manual/close")
def manual_close(user: User = Depends(current_user)):
    return manager.close_manual()

class BacktestIn(BaseModel):
    from_date: str
    to_date: str
    symbol: str = "XAUUSD.sd"
    timeframe: str = "M1"
    lot: float = Field(default=0.01, gt=0, le=100)
    max_trades_per_day: int = Field(default=0, ge=0, le=100)
    daily_target_points: float = Field(default=0, ge=0, le=100000)

@router.post("/backtest/strategic")
def strategic_backtest(payload: BacktestIn, user: User = Depends(current_user)):
    return manager.strategic_backtest(
        payload.from_date,
        payload.to_date,
        payload.lot,
        payload.max_trades_per_day,
        payload.daily_target_points,
        payload.symbol,
        payload.timeframe,
    )

class TimingBacktestIn(BaseModel):
    from_date: str
    to_date: str
    symbol: str = "XAUUSD.sd"
    timeframe: str = "M5"
    lot: float = Field(default=0.01, gt=0, le=100)
    max_trades_per_day: int = Field(default=0, ge=0, le=100)
    daily_target_points: float = Field(default=0, ge=0, le=100000)
    timing_hour: int = Field(default=9, ge=0, le=23)
    timing_minute: int = Field(default=30, ge=0, le=59)

@router.post("/backtest/timing")
def timing_backtest(payload: TimingBacktestIn, user: User = Depends(current_user)):
    return manager.timing_backtest(
        payload.from_date, payload.to_date, payload.lot,
        payload.max_trades_per_day, payload.daily_target_points,
        payload.symbol, payload.timeframe, payload.timing_hour, payload.timing_minute
    )

@router.get("/account")
def account(user: User = Depends(current_user)):
    return manager.mt5_test()
