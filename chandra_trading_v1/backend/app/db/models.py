from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Text
from datetime import datetime
from .database import Base

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    google_id = Column(String(255), unique=True, nullable=False)
    email = Column(String(320), unique=True, nullable=False)
    name = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_login = Column(DateTime, default=datetime.utcnow, nullable=False)

class BrokerAccount(Base):
    __tablename__ = "broker_accounts"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    broker_name = Column(String(100), default="Equiti MT5")
    mt5_login = Column(String(100), nullable=False)
    mt5_server = Column(String(255), nullable=False)
    encrypted_password = Column(Text, nullable=False)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

class BotConfig(Base):
    __tablename__ = "bot_configs"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, unique=True, index=True)
    symbol = Column(String(50), default="XAUUSD")
    timeframe = Column(String(20), default="M1")
    lot = Column(Float, default=0.01)
    strategy = Column(String(30), default="buy_sell")
    running = Column(Boolean, default=False)
    live_enabled = Column(Boolean, default=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class Trade(Base):
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    broker_account_id = Column(Integer, nullable=True)
    symbol = Column(String(50), nullable=False)
    strategy = Column(String(30), nullable=False)
    side = Column(String(10), nullable=False)
    volume = Column(Float, nullable=False)
    price = Column(Float, nullable=True)
    ticket = Column(String(100), nullable=True)
    status = Column(String(30), nullable=False)
    pnl = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
