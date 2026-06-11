#!/usr/bin/env python3
"""
Создание клиента напрямую в БД (без HTTP-запроса).
Используется для первоначальной настройки и создания первого администратора.

Использование:
  python scripts/create_client.py --phone +77001234567 --name "Иван" [options]

Опции:
  --phone   Номер телефона в формате +7XXXXXXXXXX (обязательно)
  --name    Имя клиента (обязательно)
  --plan    Тариф: trial|start|business|potok|network (по умолчанию: trial)
  --days    Срок действия в днях (по умолчанию: 7)
  --admin   Дать права администратора (флаг, по умолчанию: нет)
  --password  Задать пароль вручную (по умолчанию: сгенерировать)

Требует DATABASE_URL в окружении (или .env рядом с main.py).
"""

import argparse
import asyncio
import os
import re
import secrets
import string
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Allow running from project root or scripts/ dir
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load .env if present
_env_file = ROOT / ".env"
if _env_file.exists():
    with open(_env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] in ("7", "8"):
        return "+7" + digits[1:]
    if len(digits) == 10 and digits[0] == "7":
        return "+7" + digits
    return None


def gen_password(length: int = 14) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


async def create(name: str, phone: str, plan: str, days: int, is_admin: bool, password: str | None):
    from backend.database import AsyncSessionLocal, init_db
    from backend.models.user import User
    from sqlalchemy import select
    import bcrypt as _bcrypt

    await init_db()

    phone = normalize_phone(phone)
    if not phone:
        print("❌ Неверный формат телефона. Ожидается: +7 7XX XXX XX XX")
        sys.exit(1)

    plain_pw = password or gen_password()
    hashed_pw = _bcrypt.hashpw(plain_pw.encode(), _bcrypt.gensalt()).decode()

    async with AsyncSessionLocal() as db:
        existing = await db.execute(select(User).where(User.phone == phone))
        if existing.scalar():
            print(f"❌ Телефон {phone} уже зарегистрирован.")
            sys.exit(1)

        user = User(
            name=name,
            phone=phone,
            hashed_password=hashed_pw,
            plan=plan,
            plan_expires=datetime.utcnow() + timedelta(days=days),
            is_verified=True,
            is_active=True,
            is_admin=is_admin,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

    print()
    print("✅ Клиент создан")
    print(f"   ID:      {user.id}")
    print(f"   Имя:     {user.name}")
    print(f"   Телефон: {user.phone}")
    print(f"   Тариф:   {user.plan} ({days} дн.)")
    print(f"   Админ:   {'да' if is_admin else 'нет'}")
    print()
    print(f"   Пароль:  {plain_pw}")
    print()
    print("   ⚠️  Сохраните пароль — он больше нигде не отображается.")
    print()


def main():
    parser = argparse.ArgumentParser(description="Создать клиента TrustControl")
    parser.add_argument("--phone",    required=True, help="Телефон: +77001234567")
    parser.add_argument("--name",     required=True, help="Имя клиента")
    parser.add_argument("--plan",     default="trial", help="Тариф (trial|start|business|potok|network)")
    parser.add_argument("--days",     type=int, default=7, help="Дней подписки")
    parser.add_argument("--admin",    action="store_true", help="Права администратора")
    parser.add_argument("--password", default=None, help="Задать пароль вручную")
    args = parser.parse_args()

    asyncio.run(create(
        name=args.name,
        phone=args.phone,
        plan=args.plan,
        days=args.days,
        is_admin=args.admin,
        password=args.password,
    ))


if __name__ == "__main__":
    main()
