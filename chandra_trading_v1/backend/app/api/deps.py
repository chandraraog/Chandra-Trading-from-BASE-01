from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session
from backend.app.db.database import get_db
from backend.app.db.models import User

def current_user(request: Request, db: Session = Depends(get_db)):
    user_id = request.session.get("user_id")
    if user_id:
        user = db.get(User, user_id)
        if user:
            return user
        request.session.clear()
    # V1: local single-user mode. Google authentication is intentionally deferred.
    user = db.query(User).filter(User.email == "local@chandra-trading.local").first()
    if not user:
        user = User(google_id="local-v1", email="local@chandra-trading.local", name="Local V1 User")
        db.add(user); db.commit(); db.refresh(user)
    request.session["user_id"] = user.id
    return user
