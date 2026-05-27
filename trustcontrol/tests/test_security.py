# ════════════════════════════════════════════════════════════
#  Тесты безопасности и unit-тесты анализатора
#  Самое важное — multi-tenant изоляция данных.
# ════════════════════════════════════════════════════════════

import pytest
from httpx import AsyncClient

from tests.conftest import auth_headers, register_user


@pytest.mark.asyncio
class TestAuth:
    """Тесты авторизации (phone + OTP)."""

    async def test_register_sends_otp(self, client: AsyncClient):
        r = await client.post("/api/auth/register", json={
            "name":     "Данил",
            "phone":    "+77001112201",
            "password": "strongpass123",
        })
        assert r.status_code == 200
        data = r.json()
        # OTP_BYPASS=true → возвращается otp_code, статус otp_sent
        assert data["status"] == "otp_sent"
        assert data["otp_code"] == "000000"

    async def test_verify_otp_returns_token(self, client: AsyncClient):
        data = await register_user(client)
        assert "access_token" in data
        assert data["plan"] == "trial"

    async def test_register_weak_password(self, client: AsyncClient):
        """Пароль меньше 8 символов — ошибка валидации."""
        r = await client.post("/api/auth/register", json={
            "name":     "Test",
            "phone":    "+77001112202",
            "password": "123",
        })
        assert r.status_code == 422

    async def test_register_duplicate_phone(self, client: AsyncClient):
        """Нельзя зарегистрироваться повторно с тем же номером после верификации."""
        phone = "+77001112203"
        await register_user(client, phone=phone)  # верифицирован

        r = await client.post("/api/auth/register", json={
            "name":     "User",
            "phone":    phone,
            "password": "strongpass123",
        })
        assert r.status_code == 400

    async def test_login_wrong_password(self, client: AsyncClient):
        """Неверный пароль → 401."""
        data = await register_user(client)
        r = await client.post("/api/auth/login", data={
            "username": data["phone"],
            "password": "wrongpassword",
        })
        assert r.status_code == 401

    async def test_access_without_token(self, client: AsyncClient):
        """Нельзя получить данные без токена."""
        r = await client.get("/api/locations/")
        assert r.status_code == 401

    async def test_invalid_token_rejected(self, client: AsyncClient):
        r = await client.get(
            "/api/locations/",
            headers={"Authorization": "Bearer fake.token.here"},
        )
        assert r.status_code == 401


@pytest.mark.asyncio
class TestDataIsolation:
    """
    КРИТИЧНЫЕ тесты — юзер А не видит данные юзера Б.
    Это самое важное для multi-tenant SaaS.
    """

    async def test_user_sees_only_own_locations(self, client: AsyncClient):
        headers_a = await auth_headers(client)
        headers_b = await auth_headers(client)

        r = await client.post("/api/locations/", json={
            "name":          "Касса юзера A",
            "business_type": "coffee",
        }, headers=headers_a)
        assert r.status_code == 200

        r = await client.get("/api/locations/", headers=headers_b)
        assert r.status_code == 200
        locations_b = r.json()
        assert all(loc["name"] != "Касса юзера A" for loc in locations_b)

    async def test_user_cannot_delete_other_location(self, client: AsyncClient):
        headers_a = await auth_headers(client)
        headers_b = await auth_headers(client)

        r = await client.post("/api/locations/", json={
            "name":          "Касса A для удаления",
            "business_type": "cafe",
        }, headers=headers_a)
        location_id = r.json()["id"]

        r = await client.delete(f"/api/locations/{location_id}", headers=headers_b)
        assert r.status_code in (403, 404)

    async def test_invalid_api_key_rejected(self, client: AsyncClient):
        """Скрипт с неверным API ключом получает 401."""
        import io, wave, struct
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(struct.pack("<h", 0) * 100)
        buf.seek(0)

        r = await client.post(
            "/api/reports/submit",
            files={"audio": ("test.wav", buf, "audio/wav")},
            headers={"X-API-Key": "fake_key_12345"},
        )
        assert r.status_code == 401


class TestAnalyzer:
    """Unit-тесты анализатора (без БД и HTTP)."""

    def test_greeting_detected(self):
        from backend.services.analyzer import analyze
        found = analyze("Добро пожаловать! Что будете заказывать?", "coffee")
        assert "✅ Приветствие" in found

    def test_kazakh_greeting_detected(self):
        from backend.services.analyzer import analyze
        found = analyze("Қош келдіңіз! Не қалайсыз?", "coffee")
        assert "✅ Приветствие" in found

    def test_thanks_detected(self):
        from backend.services.analyzer import analyze
        found = analyze("Спасибо большое! Приятного дня.", "coffee")
        assert "✅ Благодарность" in found

    def test_goodbye_detected(self):
        from backend.services.analyzer import analyze
        found = analyze("Хорошего дня! До свидания.", "coffee")
        assert "✅ Прощание" in found

    def test_get_tone_passthrough(self):
        """get_tone(gpt_tone, events) — основа GPT-тон."""
        from backend.services.analyzer import get_tone
        assert get_tone("positive") == "positive"
        assert get_tone("negative") == "negative"
        assert get_tone("neutral") == "neutral"

    def test_get_tone_fallback_on_fraud(self):
        from backend.services.analyzer import get_tone
        assert get_tone("", events={"fraud_attempt": True}) == "negative"

    def test_score_fraud_penalty(self):
        """fraud → score ≤ 10 (жёсткое бизнес-правило)."""
        from backend.services.analyzer import calculate_score
        score = calculate_score(
            gpt_score=50,
            events={"fraud_attempt": True},
            has_fraud=True,
        )
        assert score < 15

    def test_score_normal_conversation(self):
        """Нормальный разговор без флагов — score ≥ 50."""
        from backend.services.analyzer import calculate_score
        score = calculate_score(
            gpt_score=80,
            events={"greeting": True, "farewell": True},
            has_greeting=True,
            has_goodbye=True,
            tone="positive",
        )
        assert score >= 70

    def test_score_no_gpt_fallback(self):
        """Без gpt_score — fallback через флаги, база 50."""
        from backend.services.analyzer import calculate_score
        score = calculate_score(
            gpt_score=None,
            has_greeting=True,
            has_goodbye=True,
            has_bonus=True,
            tone="positive",
        )
        assert score >= 80

    def test_target_upsells_coffee(self):
        """Целевые допродажи возвращаются для coffee."""
        from backend.services.analyzer import get_target_upsells
        targets = get_target_upsells("coffee")
        assert "сироп" in targets
        assert "карта лояльности" in targets

    def test_target_upsells_custom_phrases(self):
        from backend.services.analyzer import get_target_upsells
        targets = get_target_upsells("coffee", custom_phrases=["мой_бонус"])
        assert "мой_бонус" in targets


@pytest.mark.asyncio
class TestHealthCheck:
    async def test_health(self, client: AsyncClient):
        r = await client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
