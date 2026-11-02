from datetime import datetime, timedelta, timezone
from jose import jwt, JWTError
from passlib.context import CryptContext
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from .config import settings
from .db import get_db
from .models import User, Role

pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2 = OAuth2PasswordBearer(tokenUrl="/api/login")

def hash_password(value): return pwd.hash(value)
def verify_password(value, hashed): return bool(hashed) and pwd.verify(value, hashed)

def make_token(user):
    exp = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    return jwt.encode({"sub": str(user.id), "role": user.role.value, "exp": exp}, settings.jwt_secret, algorithm="HS256")

async def current_user(token: str = Depends(oauth2), db: AsyncSession = Depends(get_db)):
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        uid = int(payload["sub"])
    except (JWTError, ValueError, KeyError):
        raise HTTPException(401, "Недействительный токен")
    user = await db.get(User, uid)
    if not user or not user.active:
        raise HTTPException(401, "Пользователь не найден или отключён")
    return user

def require_roles(*roles):
    async def checker(user: User = Depends(current_user)):
        if user.role not in roles:
            raise HTTPException(403, "Недостаточно прав")
        return user
    return checker

manager_only = require_roles(Role.manager)
foreman_or_manager = require_roles(Role.foreman, Role.manager)
