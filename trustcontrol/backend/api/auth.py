# ════════════════════════════════════════════════════════════
#  API: Авторизация v2.0 — OTP Email Verification
#
#  Флоу регистрации:
#    POST /register  → создаёт юзера (is_verified=False), шлёт OTP на почту
#    POST /verify-otp → проверяет OTP, ставит is_verified=True, возвращает JWT
#
#  Флоу входа:
#    POST /login     → email+password, если is_verified=False → 403 EMAIL_NOT_VERIFIED
#    POST /send-otp  → (переотправка) генерирует и шлёт новый код
#
#  SECURITY:
#    - Rate limit: 5 попыток логина / 60 сек
#    - Пароль: минимум 8 символов
#    - Email: EmailStr (Pydantic)
#    - Телефон: нормализация в +7XXXXXXXXXX (KZ формат)
#    - OTP: 6 цифр, 10 минут, одноразовый
# ════════════════════════════════════════════════════════════

import re
import time
import random
import string
from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update as sa_update
from pydantic import BaseModel, EmailStr, field_validator
from passlib.context import CryptContext
from jose import JWTError, jwt

from backend.database import get_db
from backend.models.user import User
from backend.models.otp_code import OtpCode
from backend.services.email_sender import send_otp_email
from backend.config import settings

router    = APIRouter()
pwd_ctx   = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2    = OAuth2PasswordBearer(tokenUrl="/api/auth/login")
ALGORITHM = "HS256"

# ── Rate limiting ─────────────────────────────────────────────────────────────
_login_attempts: dict[str, list[float]] = defaultdict(list)
MAX_ATTEMPTS   = 5
WINDOW_SECONDS = 60


def _check_rate_limit(ip: str):
    now = time.time()
    _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < WINDOW_SECONDS]
    if len(_login_attempts[ip]) >= MAX_ATTEMPTS:
        raise HTTPException(
            status_code=429,
            detail=f"Слишком много попыток. Подождите {WINDOW_SECONDS} секунд.",
        )
    _login_attempts[ip].append(now)


# ── Phone helpers ─────────────────────────────────────────────────────────────

def normalize_phone(raw: str) -> str | None:
    """Normalise any KZ phone string → +7XXXXXXXXXX, or None if invalid."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] in ("7", "8"):
        return "+7" + digits[1:]
    if len(digits) == 10 and digits[0] == "7":
        return "+7" + digits
    return None


# ── OTP helpers ───────────────────────────────────────────────────────────────

def _generate_otp() -> str:
    return "".join(random.choices(string.digits, k=6))


async def _create_and_send_otp(email: str, name: str, db: AsyncSession) -> None:
    """Invalidate old codes, generate a new one and email it."""
    await db.execute(
        sa_update(OtpCode)
        .where(OtpCode.email == email, OtpCode.used == False)  # noqa: E712
        .values(used=True)
    )
    code = _generate_otp()
    db.add(OtpCode(
        email=email,
        code=code,
        expires_at=datetime.utcnow() + timedelta(minutes=10),
    ))
    await db.flush()
    await send_otp_email(email, code, name)


# ── Pydantic schemas ──────────────────────────────────────────────────────────

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

    @field_validator("phone")
    @classmethod
    def phone_format(cls, v):
        normed = normalize_phone(v)
        if not normed:
            raise ValueError("Неверный формат. Ожидается: +7 7XX XXX XX XX")
        return normed


class OtpSendRequest(BaseModel):
    email: EmailStr


class OtpVerifyRequest(BaseModel):
    email: EmailStr
    code:  str


class TokenResponse(BaseModel):
    access_token: str
    token_type:   str = "bearer"
    user_id:      int
    name:         str
    plan:         str


# ── JWT utils ─────────────────────────────────────────────────────────────────

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


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/register")
async def register(data: RegisterRequest, db: AsyncSession = Depends(get_db)):
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
        is_verified=False,
    )
    db.add(user)
    await db.flush()

    await _create_and_send_otp(data.email, data.name, db)
    await db.commit()

    return {"status": "otp_sent", "email": data.email}


@router.post("/send-otp")
async def send_otp(data: OtpSendRequest, db: AsyncSession = Depends(get_db)):
    """Resend (or first-send) OTP for an existing unverified account."""
    result = await db.execute(select(User).where(User.email == data.email))
    user = result.scalar()
    if not user:
        # Don't reveal whether email exists
        return {"status": "sent"}

    await _create_and_send_otp(data.email, user.name, db)
    await db.commit()
    return {"status": "sent"}


@router.post("/verify-otp", response_model=TokenResponse)
async def verify_otp(data: OtpVerifyRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(OtpCode).where(
            OtpCode.email      == data.email,
            OtpCode.code       == data.code.strip(),
            OtpCode.used       == False,           # noqa: E712
            OtpCode.expires_at >  datetime.utcnow(),
        )
    )
    otp = result.scalar()
    if not otp:
        raise HTTPException(status_code=400, detail="Неверный или просроченный код")

    otp.used = True

    result = await db.execute(select(User).where(User.email == data.email))
    user = result.scalar()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    user.is_verified = True
    user.last_login  = datetime.utcnow()
    await db.commit()

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
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(client_ip)

    result = await db.execute(select(User).where(User.email == form.username))
    user = result.scalar()

    if not user or not verify_password(form.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Неверный email или пароль")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Аккаунт заблокирован")

    if not user.is_verified:
        # Auto-resend so the user can verify without extra steps
        await _create_and_send_otp(user.email, user.name, db)
        await db.commit()
        raise HTTPException(status_code=403, detail="EMAIL_NOT_VERIFIED")

    user.last_login = datetime.utcnow()
    _login_attempts[client_ip] = []
    await db.commit()

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
        "is_verified":     user.is_verified,
        "plan_expires":    user.plan_expires.isoformat() if user.plan_expires else None,
        "is_trial_active": (
            user.plan == "trial"
            and user.plan_expires is not None
            and user.plan_expires > datetime.utcnow()
        ),
    }
