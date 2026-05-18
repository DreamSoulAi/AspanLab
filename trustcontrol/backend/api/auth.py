# ════════════════════════════════════════════════════════════
#  API: Авторизация v3.1 — Phone OTP
#
#  Флоу регистрации:
#    POST /register  → создаёт юзера (is_verified=False)
#    POST /verify-otp → проверяет OTP, ставит is_verified=True, возвращает JWT
#
#  Флоу входа:
#    POST /login     → phone+password, если is_verified=False → 403 PHONE_NOT_VERIFIED
#    POST /send-otp  → (переотправка) генерирует новый код
#
#  SECURITY:
#    - Rate limit: 5 попыток / 60 сек на все auth-эндпоинты
#    - Пароль: минимум 8 символов
#    - Телефон: нормализация в +7XXXXXXXXXX (KZ формат), уникальный ключ
#    - OTP: 6 цифр, secrets.randbelow (CSPRNG), 10 минут, одноразовый
#    - otp_code НЕ возвращается в продакшн-ответах (только OTP_BYPASS=true)
# ════════════════════════════════════════════════════════════

import re
import time
import secrets
import traceback
import logging
from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update as sa_update
from pydantic import BaseModel, field_validator, Field
import bcrypt as _bcrypt
from jose import JWTError, jwt

from backend.database import get_db
from backend.models.user import User
from backend.models.otp_code import OtpCode
from backend.config import settings

_log    = logging.getLogger("auth")
router  = APIRouter()
oauth2  = OAuth2PasswordBearer(tokenUrl="/api/auth/login")
ALGORITHM = "HS256"

# ── Rate limiting ──────────────────────────────────────────────────────────────
_login_attempts: dict[str, list[float]] = defaultdict(list)
MAX_ATTEMPTS    = 5
WINDOW_SECONDS  = 60


def _check_rate_limit(ip: str):
    now = time.time()
    _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < WINDOW_SECONDS]
    if len(_login_attempts[ip]) >= MAX_ATTEMPTS:
        raise HTTPException(
            status_code=429,
            detail=f"Слишком много попыток. Подождите {WINDOW_SECONDS} секунд.",
        )
    _login_attempts[ip].append(now)
    # Prune empty entries to prevent memory growth from unique IPs
    if len(_login_attempts[ip]) == 0:
        del _login_attempts[ip]


# ── Phone helpers ──────────────────────────────────────────────────────────────

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
    if settings.OTP_BYPASS:
        return "000000"
    return f"{secrets.randbelow(1_000_000):06d}"


async def _create_and_send_otp(phone: str, name: str, db: AsyncSession) -> str:
    """Invalidate old codes, generate a new one. Returns the code."""
    await db.execute(
        sa_update(OtpCode)
        .where(OtpCode.phone == phone, OtpCode.used == False)  # noqa: E712
        .values(used=True)
    )
    code = _generate_otp()
    db.add(OtpCode(
        phone=phone,
        code=code,
        expires_at=datetime.utcnow() + timedelta(minutes=10),
    ))
    await db.flush()

    # Only log the code in bypass/dev mode — never in production
    if settings.OTP_BYPASS:
        _log.info(f"OTP (bypass): phone={phone} code={code}")
    else:
        _log.info(f"OTP generated: phone={phone}")
    # Future: await send_sms(phone, code, name)
    return code


# ── Pydantic schemas ───────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    name:     str = Field(..., min_length=2, max_length=100)
    phone:    str
    password: str
    email:    str = Field("", max_length=150)

    @field_validator("password")
    @classmethod
    def password_strength(cls, v):
        if len(v) < 8:
            raise ValueError("Пароль должен быть минимум 8 символов")
        return v

    @field_validator("phone")
    @classmethod
    def phone_format(cls, v):
        normed = normalize_phone(v)
        if not normed:
            raise ValueError("Неверный формат. Ожидается: +7 7XX XXX XX XX")
        return normed


class OtpSendRequest(BaseModel):
    phone: str


class OtpVerifyRequest(BaseModel):
    phone: str
    code:  str = Field(..., min_length=6, max_length=6)


class TokenResponse(BaseModel):
    access_token: str
    token_type:   str = "bearer"
    user_id:      int
    name:         str
    plan:         str


# ── JWT utils ─────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


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

@router.get("/app-config")
async def app_config():
    """Public config returned to frontend on load."""
    return {
        "tg_bot_username": settings.TELEGRAM_BOT_USERNAME,
    }


@router.post("/register")
async def register(data: RegisterRequest, request: Request, db: AsyncSession = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(client_ip)
    try:
        existing = await db.execute(select(User).where(User.phone == data.phone))
        existing_user = existing.scalar()

        if existing_user:
            if existing_user.is_verified:
                raise HTTPException(status_code=400, detail="Номер уже зарегистрирован")
            existing_user.name            = data.name
            existing_user.email           = data.email or None
            existing_user.hashed_password = hash_password(data.password)
            code = await _create_and_send_otp(data.phone, data.name, db)
            await db.commit()
            resp = {"status": "otp_sent", "phone": data.phone}
            if settings.OTP_BYPASS:
                resp["otp_code"] = code
            return resp

        user = User(
            name=data.name,
            phone=data.phone,
            email=data.email or None,
            hashed_password=hash_password(data.password),
            plan="trial",
            plan_expires=datetime.utcnow() + timedelta(days=14),
            is_verified=False,
        )
        db.add(user)
        await db.flush()

        code = await _create_and_send_otp(data.phone, data.name, db)
        await db.commit()

        resp = {"status": "otp_sent", "phone": data.phone}
        if settings.OTP_BYPASS:
            resp["otp_code"] = code
        return resp

    except HTTPException:
        raise
    except Exception:
        _log.error(f"register 500: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail="Внутренняя ошибка. Попробуйте позже.")


@router.post("/send-otp")
async def send_otp(data: OtpSendRequest, request: Request, db: AsyncSession = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(client_ip)

    phone = normalize_phone(data.phone) or data.phone.strip()
    result = await db.execute(select(User).where(User.phone == phone))
    user = result.scalar()
    if not user:
        return {"status": "sent"}  # Don't reveal whether phone exists

    code = await _create_and_send_otp(phone, user.name, db)
    await db.commit()

    resp = {"status": "sent"}
    if settings.OTP_BYPASS:
        resp["otp_code"] = code
    return resp


@router.post("/verify-otp", response_model=TokenResponse)
async def verify_otp(data: OtpVerifyRequest, request: Request, db: AsyncSession = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(client_ip)

    phone = normalize_phone(data.phone) or data.phone.strip()

    result = await db.execute(
        select(OtpCode).where(
            OtpCode.phone      == phone,
            OtpCode.code       == data.code.strip(),
            OtpCode.used       == False,           # noqa: E712
            OtpCode.expires_at >  datetime.utcnow(),
        )
    )
    otp = result.scalar()
    if not otp:
        raise HTTPException(status_code=400, detail="Неверный или просроченный код")

    otp.used = True

    result = await db.execute(select(User).where(User.phone == phone))
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

    phone = normalize_phone(form.username) or form.username.strip()

    result = await db.execute(select(User).where(User.phone == phone))
    user = result.scalar()

    if not user or not verify_password(form.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Неверный номер или пароль")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Аккаунт заблокирован")

    if not user.is_verified:
        await _create_and_send_otp(user.phone, user.name, db)
        await db.commit()
        raise HTTPException(status_code=403, detail="PHONE_NOT_VERIFIED")

    user.last_login = datetime.utcnow()
    _login_attempts.pop(client_ip, None)
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
        "phone":           user.phone,
        "email":           user.email or "",
        "telegram_chat":   user.telegram_chat or "",
        "plan":            user.plan,
        "is_verified":     user.is_verified,
        "plan_expires":    user.plan_expires.isoformat() if user.plan_expires else None,
        "is_trial_active": (
            user.plan == "trial"
            and user.plan_expires is not None
            and user.plan_expires > datetime.utcnow()
        ),
    }


class UpdateMeRequest(BaseModel):
    name:          str | None = Field(None, max_length=100)
    email:         str | None = Field(None, max_length=150)
    telegram_chat: str | None = Field(None, max_length=50)
    password:      str | None = None


@router.patch("/me")
async def update_me(
    data: UpdateMeRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if data.name is not None:
        user.name = data.name.strip()
    if data.email is not None:
        user.email = data.email.strip() or None
    if data.telegram_chat is not None:
        user.telegram_chat = data.telegram_chat.strip() or None
    if data.password:
        if len(data.password) < 8:
            raise HTTPException(status_code=400, detail="Пароль минимум 8 символов")
        user.hashed_password = hash_password(data.password)
    await db.commit()
    return {"status": "ok"}
