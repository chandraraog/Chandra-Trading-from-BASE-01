from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
import httpx
from backend.app.core.config import settings
from backend.app.db.database import get_db
from backend.app.db.models import User

router = APIRouter(prefix="/api/auth", tags=["auth"])

@router.get("/google")
def google_login():
    if not settings.google_client_id:
        raise HTTPException(500, "Google OAuth is not configured")
    from urllib.parse import urlencode
    params = urlencode({
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "select_account",
    })
    return RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + params)

@router.get("/google/callback")
async def google_callback(code: str, request: Request, db: Session = Depends(get_db)):
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(500, "Google OAuth is not configured")

    async with httpx.AsyncClient(timeout=20) as client:
        token = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "redirect_uri": settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        token.raise_for_status()
        access_token = token.json()["access_token"]
        info = await client.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        info.raise_for_status()
        profile = info.json()

    user = db.query(User).filter(User.google_id == profile["sub"]).first()
    if not user:
        user = User(
            google_id=profile["sub"],
            email=profile["email"],
            name=profile.get("name"),
        )
        db.add(user)
    user.last_login = datetime.utcnow()
    db.commit()
    db.refresh(user)

    request.session["user_id"] = user.id
    return RedirectResponse("/")

@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}
