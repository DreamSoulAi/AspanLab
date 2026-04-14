# ════════════════════════════════════════════════════════════
#  API: Авторизация
#  SECURITY FIXES:
#  - Минимальная длина пароля 8 символов
#  - Rate limiting на логин (5 попыток / 60 сек)
#  - Валидация email формата
#  - Нет утечки информации при неверном логине
# ════════════════════════════════════════════════════════════

import time
from collections import defaultdict
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, EmailStr, field_validator
from passlib.context import CryptContext
from jose import JWTError, jwt
from datetime import datetime, timedelta

from backend.database import get_db
from backend.models.user import User
from backend.config import settings

router   = APIRouter()
pwd_ctx  = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2   = OAuth2PasswordBearer(tokenUrl="/api/auth/login")
ALGORITHM = "HS256"

# ── Rate limiting: храним попытки в памяти ───────────────────
# { ip: [(timestamp, ...), ...] }
_login_attempts: dict[str, list[float]] = defaultdict(list)
MAX_ATTEMPTS    = 5    # попыток
WINDOW_SECONDS  = 60   # за 60 секунд


def _check_rate_limit(ip: str):
    now = time.time()
    # Убираем старые попытки
    _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < WINDOW_SECONDS]
    if len(_login_attempts[ip]) >= MAX_ATTEMPTS:
        raise HTTPException(
            status_code=429,
            detail=f"Слишком много попыток входа. Подождите {WINDOW_SECONDS} секунд."
        )
    _login_attempts[ip].append(now)


# ── Схемы ────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    name:     str
    email:    EmailStr
    phone:    str
    password: str

    @field_validator("password")
    @classmethod
    def password_strength(cls, v):
        if len(v) < 8:
            raise ValueError("Пароль должен быть минимум 8 символов")
        return v

    @field_validator("name")
    @classmethod
    def name_length(cls, v):
        if len(v.strip()) < 2:
            raise ValueError("Имя слишком короткое")
        return v.strip()


class TokenResponse(BaseModel):
    access_token: str
    token_type:   str = "bearer"
    user_id:      int
    name:         str
    plan:         str


# ── Утилиты ──────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return pwd_ctx.hash(password)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_ctx.verify(plain, hashed)

def create_token(user_id: int) -> str:
    expire = datetime.utcnow() + timedelta(hours=settings.TOKEN_EXPIRE_HOURS)
    return jwt.encode(
        {"sub": str(user_id), "exp": expire},
        settings.SECRET_KEY,
        algorithm=ALGORITHM,
    )

async def get_current_user(
    token: str = Depends(oauth2),
    db: AsyncSession = Depends(get_db),
) -> User:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise HTTPException(status_code=401, detail="Неверный или просроченный токен")

    user = await db.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="Пользователь не найден")
    return user


# ── Роуты ────────────────────────────────────────────────────

@router.post("/register", response_model=TokenResponse)
async def register(data: RegisterRequest, db: AsyncSession = Depends(get_db)):
    # Проверяем уникальность email
    existing = await db.execute(select(User).where(User.email == data.email))
    if existing.scalar():
        raise HTTPException(status_code=400, detail="Email уже зарегистрирован")

    user = User(
        name=data.name,
        email=data.email,
        phone=data.phone,
        hashed_password=hash_password(data.password),
        plan="trial",
        plan_expires=datetime.utcnow() + timedelta(days=14),
    )
    db.add(user)
    await db.flush()

    return TokenResponse(
        access_token=create_token(user.id),
        user_id=user.id,
        name=user.name,
        plan=user.plan,
    )


@router.post("/login", response_model=TokenResponse)
async def login(
    request: Request,
    form: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    # ── SECURITY: rate limiting по IP ───────────────────────
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(client_ip)

    result = await db.execute(select(User).where(User.email == form.username))
    user = result.scalar()

    # ── SECURITY: одно сообщение об ошибке (нет утечки инфо)
    if not user or not verify_password(form.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Неверный email или пароль")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Аккаунт заблокирован")

    user.last_login = datetime.utcnow()

    # Успешный логин — сбрасываем счётчик попыток
    _login_attempts[client_ip] = []

    return TokenResponse(
        access_token=create_token(user.id),
        user_id=user.id,
        name=user.name,
        plan=user.plan,
    )


@router.get("/me")
async def me(user: User = Depends(get_current_user)):
    return {
        "id":              user.id,
        "name":            user.name,
        "email":           user.email,
        "phone":           user.phone,
        "plan":            user.plan,
        "plan_expires":    user.plan_expires.isoformat() if user.plan_expires else None,
        "is_trial_active": user.plan == "trial" and user.plan_expires and user.plan_expires > datetime.utcnow(),
    }
