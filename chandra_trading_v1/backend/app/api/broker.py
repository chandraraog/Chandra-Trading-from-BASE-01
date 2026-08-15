from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from backend.app.api.deps import current_user
from backend.app.core.security import encrypt_secret
from backend.app.db.database import get_db
from backend.app.db.models import BrokerAccount, User

router = APIRouter(prefix="/api/broker", tags=["broker"])

class BrokerIn(BaseModel):
    mt5_login: str
    mt5_server: str
    mt5_password: str = Field(min_length=1)

@router.post("/equiti")
def save_equiti(payload: BrokerIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    row = db.query(BrokerAccount).filter(BrokerAccount.user_id == user.id).first()
    if not row:
        row = BrokerAccount(user_id=user.id)
        db.add(row)
    row.broker_name = "Equiti MT5"
    row.mt5_login = payload.mt5_login
    row.mt5_server = payload.mt5_server
    row.encrypted_password = encrypt_secret(payload.mt5_password)
    row.active = True
    db.commit()
    return {"ok": True, "message": "Encrypted broker credentials saved"}
