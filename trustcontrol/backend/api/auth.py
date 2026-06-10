# ════════════════════════════════════════════════════════════
#  API: Авторизация v4.0 — Phone + Password only
#
#  Флоу входа:
#    POST /login  → phone+password → JWT
#
#  Регистрация: закрыта. Только администратор создаёт клиентов:
#    POST /admin/create-client → генерирует пароль, возвращает однократно
#    CLI: scripts/create_client.py для первого клиента/админа
#
#  SECURITY:
#    - Rate limit: 5 попыток / 60 сек на все auth-эндпоинты
#    - Пароль: минимум 8 символов
#    - Телефон: нормализация в +7XXXXXXXXXX (KZ формат), уникальный ключ
#    - OtpCode модель остаётся — используется для Telegram-линковки (/tg-link)
# ════════════════════════════════════════════════════════════

import os
import re
import time
import string
import hashlib
import secrets
import traceback
import logging
from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update as sa_update
from pydantic import BaseModel, EmailStr, field_validator, Field
import bcrypt as _bcrypt
from jose import JWTError, jwt

from backend.database import get_db
from backend.models.user import User
from backend.models.otp_code import OtpCode
from backend.config import settings
from backend.services.subscription import TRIAL_DAYS

_log    = logging.getLogger("auth")
router  = APIRouter()
oauth2  = OAuth2PasswordBearer(tokenUrl="/api/auth/login")
ALGORITHM = "HS256"

# ── Rate limiting ──────────────────────────────────────────────────────────────
_login_attempts: dict[str, list[float]] = defaultdict(list)

MAX_ATTEMPTS    = 5
WINDOW_SECONDS  = 60


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        # Take the RIGHTMOST entry — added by Render's trusted proxy.
        # Client controls left entries (can spoof them); rightmost is injected by the proxy.
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def _check_rate_limit(ip: str):
    now = time.time()
    window_start = now - WINDOW_SECONDS
    # Prune all stale IPs from the dict on every call to prevent memory growth
    stale = [k for k, v in _login_attempts.items() if not v or max(v) < window_start]
    for k in stale:
        del _login_attempts[k]

    recent = [t for t in _login_attempts[ip] if t > window_start]
    if len(recent) >= MAX_ATTEMPTS:
        raise HTTPException(
            status_code=429,
            detail=f"Слишком много попыток. Подождите {WINDOW_SECONDS} секунд.",
        )
    recent.append(now)
    _login_attempts[ip] = recent


# ── Phone helpers ──────────────────────────────────────────────────────────────

def normalize_phone(raw: str) -> str | None:
    """Normalise any KZ phone string → +7XXXXXXXXXX, or None if invalid."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] in ("7", "8"):
        return "+7" + digits[1:]
    if len(digits) == 10 and digits[0] == "7":
        return "+7" + digits
    return None


# ── OTP hash helper (used for Telegram account linking) ───────────────────────

def _hash_otp(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


# ── Pydantic schemas ───────────────────────────────────────────────────────────

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


async def require_active_subscription(
    user: User = Depends(get_current_user),
) -> User:
    """Like get_current_user but blocks if subscription expired beyond grace."""
    from backend.services.subscription import get_status as _sub_status
    if _sub_status(user) == "blocked":
        raise HTTPException(
            status_code=402,
            detail="Подписка истекла. Оплатите для продолжения работы.",
        )
    return user


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/app-config")
async def app_config():
    """Public config returned to frontend on load."""
    return {
        "tg_bot_username":  settings.TELEGRAM_BOT_USERNAME,
        "kaspi_number":     settings.KASPI_NUMBER or "",
        "kaspi_name":       settings.KASPI_NAME or "TrustControl",
        "support_telegram": os.getenv("SUPPORT_TELEGRAM", "trustcontrol_support"),
    }


@router.post("/login", response_model=TokenResponse)
async def login(
    request: Request,
    form: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    client_ip = _get_client_ip(request)
    _check_rate_limit(client_ip)

    phone = normalize_phone(form.username) or form.username.strip()

    result = await db.execute(select(User).where(User.phone == phone))
    user = result.scalar()

    if not user or not verify_password(form.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Неверный номер или пароль")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Аккаунт заблокирован")

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
async def me(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from backend.services.subscription import get_status as _sub_status, days_left
    from backend.services.notifier import last_telegram_error
    from backend.api.reports import _get_monthly_count, _PLAN_MONTHLY_LIMITS
    from sqlalchemy import select as _sel
    from backend.models.location import Location as _Loc

    locs_r = await db.execute(_sel(_Loc.id).where(_Loc.owner_id == user.id))
    loc_ids = [r[0] for r in locs_r.all()]
    conversations_used = await _get_monthly_count(user.id, loc_ids) if loc_ids else 0
    conversations_limit = _PLAN_MONTHLY_LIMITS.get(user.plan or "trial", _PLAN_MONTHLY_LIMITS["trial"])

    return {
        "id":                  user.id,
        "name":                user.name,
        "company_name":        user.company_name or "",
        "phone":               user.phone,
        "email":               user.email or "",
        "telegram_chat":       user.telegram_chat or "",
        "plan":                user.plan,
        "is_admin":            bool(user.is_admin),
        "is_verified":         user.is_verified,
        "plan_expires":        user.plan_expires.isoformat() if user.plan_expires else None,
        "subscription_status": _sub_status(user),  # active | grace | blocked
        "days_left":           days_left(user),
        "is_trial_active": (
            user.plan == "trial"
            and user.plan_expires is not None
            and user.plan_expires > datetime.utcnow()
        ),
        "telegram_health": (
            last_telegram_error
            if last_telegram_error.get("chat_id") == (user.telegram_chat or "")
            else {"at": None, "msg": None, "chat_id": None}
        ),
        "conversations_used":  conversations_used,
        "conversations_limit": conversations_limit,
    }


class ExtendSubscriptionRequest(BaseModel):
    phone: str = Field(..., max_length=20)
    days:  int = Field(..., ge=1, le=365)
    plan:  str | None = Field(None, max_length=20)


@router.post("/admin/extend-subscription")
async def admin_extend_subscription(
    data: ExtendSubscriptionRequest,
    admin: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Продлить подписку клиенту вручную (после получения оплаты Kaspi)."""
    if not admin.is_admin:
        raise HTTPException(status_code=403, detail="Только для администратора")

    phone = normalize_phone(data.phone) or data.phone.strip()
    result = await db.execute(select(User).where(User.phone == phone))
    target = result.scalar()
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    from backend.services.subscription import extend
    extend(target, data.days)
    if data.plan:
        target.plan = data.plan
    target.last_subscription_reminder = None  # reset, чтобы можно было снова напомнить
    await db.commit()

    return {
        "status":       "ok",
        "user_id":      target.id,
        "phone":        target.phone,
        "plan":         target.plan,
        "plan_expires": target.plan_expires.isoformat() if target.plan_expires else None,
    }


class CreateClientRequest(BaseModel):
    name:     str  = Field(..., min_length=2, max_length=100)
    phone:    str
    plan:     str  = Field("trial", max_length=20)
    days:     int  = Field(TRIAL_DAYS, ge=1, le=3650)
    is_admin: bool = False

    @field_validator("phone")
    @classmethod
    def phone_fmt(cls, v):
        normed = normalize_phone(v)
        if not normed:
            raise ValueError("Неверный формат телефона. Ожидается: +7 7XX XXX XX XX")
        return normed


def _gen_password(length: int = 14) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


@router.post("/admin/create-client")
async def admin_create_client(
    data: CreateClientRequest,
    admin: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Создать клиента вручную (только администратор).
    Возвращает сгенерированный пароль ОДНОКРАТНО — сохраните его сразу.
    """
    if not admin.is_admin:
        raise HTTPException(status_code=403, detail="Только для администратора")

    existing = await db.execute(select(User).where(User.phone == data.phone))
    if existing.scalar():
        raise HTTPException(status_code=400, detail="Телефон уже зарегистрирован")

    password = _gen_password()
    user = User(
        name=data.name,
        phone=data.phone,
        hashed_password=hash_password(password),
        plan=data.plan,
        plan_expires=datetime.utcnow() + timedelta(days=data.days),
        is_verified=True,
        is_active=True,
        is_admin=data.is_admin,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    _log.info(f"admin_create_client: id={user.id} phone={data.phone} by admin={admin.id}")
    return {
        "status":       "ok",
        "user_id":      user.id,
        "phone":        user.phone,
        "name":         user.name,
        "plan":         user.plan,
        "plan_expires": user.plan_expires.isoformat() if user.plan_expires else None,
        "password":     password,
    }


class UpdateMeRequest(BaseModel):
    name:          str | None      = Field(None, max_length=100)
    company_name:  str | None      = Field(None, max_length=150)
    email:         EmailStr | None = None
    telegram_chat: str | None      = Field(None, max_length=50)
    password:      str | None      = None


@router.patch("/me")
async def update_me(
    data: UpdateMeRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if data.name is not None:
        user.name = data.name.strip()
    if data.company_name is not None:
        user.company_name = data.company_name.strip() or None
    if data.email is not None:
        user.email = str(data.email)  # EmailStr already validated
    if data.telegram_chat is not None:
        user.telegram_chat = data.telegram_chat.strip() or None
    if data.password:
        if len(data.password) < 8:
            raise HTTPException(status_code=400, detail="Пароль минимум 8 символов")
        user.hashed_password = hash_password(data.password)
    await db.commit()
    return {"status": "ok"}


@router.post("/tg-link")
async def tg_link(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Generate one-time Telegram deep link + manual 6-digit code for account linking."""
    from backend.api.telegram_webhook import generate_link_token

    # 6-digit manual code: показываем юзеру открытым текстом, в БД храним хеш
    code = str(secrets.randbelow(900000) + 100000)
    await db.execute(
        sa_update(OtpCode)
        .where(OtpCode.phone == f"tg:{user.id}", OtpCode.used == False)  # noqa: E712
        .values(used=True)
    )
    db.add(OtpCode(
        phone=f"tg:{user.id}",
        code=_hash_otp(code),  # хеш в БД, как и обычный OTP
        expires_at=datetime.utcnow() + timedelta(minutes=10),
    ))
    await db.commit()

    token    = generate_link_token({"type": "user", "user_id": user.id})
    bot_name = settings.TELEGRAM_BOT_USERNAME.strip()

    if not bot_name:
        try:
            from backend.services.notifier import get_bot
            me       = await get_bot().get_me()
            bot_name = me.username or ""
        except Exception as e:
            _log.error(f"tg_link get_me failed: {e}")

    if not bot_name:
        raise HTTPException(status_code=503, detail="Бот не настроен — обратитесь в поддержку")

    return {
        "url":   f"https://t.me/{bot_name}?start={token}",
        "token": token,
        "code":  code,
    }


@router.post("/tg-unlink")
async def tg_unlink(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Remove Telegram link from account."""
    user.telegram_chat = None
    user.telegram_id   = None
    await db.commit()
    return {"status": "ok"}


class TokenLoginRequest(BaseModel):
    token: str


@router.post("/token-login", response_model=TokenResponse)
async def token_login(data: TokenLoginRequest, db: AsyncSession = Depends(get_db)):
    """Login via a signed one-time token generated by the Telegram bot (/gettoken)."""
    from backend.api.telegram_webhook import _verify_link_token
    payload = _verify_link_token(data.token)
    if not payload:
        raise HTTPException(status_code=400, detail="Ссылка недействительна или истекла")
    user = await db.get(User, int(payload["user_id"]))
    if not user or not user.is_active:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    user.last_login  = datetime.utcnow()
    user.is_verified = True
    await db.commit()
    return TokenResponse(
        access_token=create_token(user.id),
        user_id=user.id,
        name=user.name,
        plan=user.plan,
    )
