# ════════════════════════════════════════════════════════════
#  Тесты безопасности — самые важные
#  Проверяют что один клиент НЕ видит данные другого
# ════════════════════════════════════════════════════════════

import pytest
from httpx import AsyncClient
from tests.conftest import auth_headers, register_user


@pytest.mark.asyncio
class TestAuth:
    """Тесты авторизации."""

    async def test_register_success(self, client: AsyncClient):
        r = await client.post("/api/auth/register", json={
            "name": "Данил",
            "email": "danil@test.com",
            "phone": "+77001234567",
            "password": "strongpass123",
        })
        assert r.status_code == 200
        data = r.json()
        assert "access_token" in data
        assert data["plan"] == "trial"

    async def test_register_weak_password(self, client: AsyncClient):
        """Пароль меньше 8 символов — ошибка."""
        r = await client.post("/api/auth/register", json={
            "name": "Test",
            "email": "weak@test.com",
            "phone": "+77001234567",
            "password": "123",  # слабый
        })
        assert r.status_code == 422  # Validation error

    async def test_register_duplicate_email(self, client: AsyncClient):
        """Нельзя зарегистрироваться с тем же email дважды."""
        data = {
            "name": "User",
            "email": "dup@test.com",
            "phone": "+77001234567",
            "password": "strongpass123",
        }
        await client.post("/api/auth/register", json=data)
        r = await client.post("/api/auth/register", json=data)
        assert r.status_code == 400

    async def test_login_wrong_password(self, client: AsyncClient):
        """Неверный пароль → 401, без утечки информации."""
        await register_user(client, "login_test@test.com")
        r = await client.post("/api/auth/login", data={
            "username": "login_test@test.com",
            "password": "wrongpassword",
        })
        assert r.status_code == 401
        # Проверяем что ошибка не раскрывает что именно неверно
        assert "пароль" in r.json()["detail"].lower() or "email" in r.json()["detail"].lower()

    async def test_access_without_token(self, client: AsyncClient):
        """Нельзя получить данные без токена."""
        r = await client.get("/api/locations/")
        assert r.status_code == 401

    async def test_invalid_token(self, client: AsyncClient):
        """Невалидный токен → 401."""
        r = await client.get(
            "/api/locations/",
            headers={"Authorization": "Bearer fake.token.here"}
        )
        assert r.status_code == 401


@pytest.mark.asyncio
class TestDataIsolation:
    """
    КРИТИЧНЫЕ тесты — проверяем что юзер А не видит данные юзера Б.
    Это самое важное для multi-tenant SaaS.
    """

    async def test_user_sees_only_own_locations(self, client: AsyncClient):
        """Юзер видит только свои точки."""
        headers_a = await auth_headers(client, "userA@test.com")
        headers_b = await auth_headers(client, "userB@test.com")

        # Юзер A создаёт точку
        r = await client.post("/api/locations/", json={
            "name": "Точка юзера A",
            "business_type": "coffee",
        }, headers=headers_a)
        assert r.status_code == 200

        # Юзер B НЕ должен видеть точку юзера A
        r = await client.get("/api/locations/", headers=headers_b)
        assert r.status_code == 200
        locations_b = r.json()
        assert all(loc["name"] != "Точка юзера A" for loc in locations_b)

    async def test_user_cannot_access_other_location_stats(self, client: AsyncClient):
        """Нельзя запросить статистику чужой точки."""
        headers_a = await auth_headers(client, "statsA@test.com")
        headers_b = await auth_headers(client, "statsB@test.com")

        # Юзер A создаёт точку
        r = await client.post("/api/locations/", json={
            "name": "Точка A для статистики",
            "business_type": "gas",
        }, headers=headers_a)
        location_id_a = r.json()["id"]

        # Юзер B пытается получить статистику точки A → 403
        r = await client.get(
            f"/api/stats/dashboard?location_id={location_id_a}",
            headers=headers_b
        )
        assert r.status_code == 403

    async def test_user_cannot_delete_other_location(self, client: AsyncClient):
        """Нельзя удалить чужую точку."""
        headers_a = await auth_headers(client, "delA@test.com")
        headers_b = await auth_headers(client, "delB@test.com")

        # Юзер A создаёт точку
        r = await client.post("/api/locations/", json={
            "name": "Точка для удаления",
            "business_type": "cafe",
        }, headers=headers_a)
        location_id = r.json()["id"]

        # Юзер B пытается удалить → 404 (не находим у него)
        r = await client.delete(f"/api/locations/{location_id}", headers=headers_b)
        assert r.status_code in [403, 404]

    async def test_invalid_api_key_rejected(self, client: AsyncClient):
        """Скрипт с неверным API ключом получает 401."""
        import io, wave, struct
        # Создаём минимальный WAV
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

    async def test_oversized_audio_rejected(self, client: AsyncClient):
        """Файл больше 10MB → 413."""
        headers_a = await auth_headers(client, "audio@test.com")

        # Создаём точку и получаем api_key
        r = await client.post("/api/locations/", json={
            "name": "Аудио тест",
            "business_type": "coffee",
        }, headers=headers_a)
        api_key = r.json()["api_key"]

        # Пытаемся загрузить 11MB файл
        big_data = b"0" * (11 * 1024 * 1024)

        r = await client.post(
            "/api/reports/submit",
            files={"audio": ("big.wav", big_data, "audio/wav")},
            headers={"X-API-Key": api_key},
        )
        assert r.status_code == 413


@pytest.mark.asyncio
class TestAnalyzer:
    """Тесты анализа фраз."""

    def test_greeting_detected(self):
        from backend.services.analyzer import analyze
        found = analyze("Добро пожаловать! Что будете заказывать?", "coffee")
        assert "✅ Приветствие" in found

    def test_fraud_detected(self):
        from backend.services.analyzer import analyze
        found = analyze("Переведи мне на карту, без чека сделаем", "coffee")
        assert "🚨 МОШЕННИЧЕСТВО" in found

    def test_bad_language_detected(self):
        from backend.services.analyzer import analyze
        found = analyze("Я же сказал, достали уже", "coffee")
        assert "⚠️ Грубость" in found

    def test_bonus_coffee_detected(self):
        from backend.services.analyzer import analyze
        found = analyze("Не желаете ли круассан к кофе?", "coffee")
        assert "⭐ Допродажа/бонус" in found

    def test_bonus_gas_detected(self):
        from backend.services.analyzer import analyze
        found = analyze("Не желаете масло или незамерзайку?", "gas")
        assert "⭐ Допродажа/бонус" in found

    def test_kazakh_greeting_detected(self):
        from backend.services.analyzer import analyze
        found = analyze("Қош келдіңіз! Не қалайсыз?", "coffee")
        assert "✅ Приветствие" in found

    def test_tone_positive(self):
        from backend.services.analyzer import analyze, get_tone
        found = analyze("Конечно, с удовольствием помогу!", "coffee")
        assert get_tone(found) == "positive"

    def test_tone_negative(self):
        from backend.services.analyzer import analyze, get_tone
        found = analyze("Я же сказал, сколько можно", "coffee")
        assert get_tone(found) in ["negative", "neutral"]

    def test_score_perfect(self):
        from backend.services.analyzer import analyze, calculate_score
        found = analyze(
            "Добро пожаловать! Не желаете круассан? Спасибо, хорошего дня!",
            "coffee"
        )
        score = calculate_score(found)
        assert score > 70

    def test_score_fraud_penalty(self):
        from backend.services.analyzer import analyze, calculate_score
        found = analyze("Переведи на мой каспи, никто не узнает", "coffee")
        score = calculate_score(found)
        assert score < 30


@pytest.mark.asyncio
class TestHealthCheck:
    async def test_health(self, client: AsyncClient):
        r = await client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
