#!/usr/bin/env python3
"""
TrustControl — Основной скрипт мониторинга качества обслуживания
Слушает микрофон → распознаёт речь → анализирует → отправляет в Telegram
"""

import os
import re
import sys
import json
import time
import wave
import queue
import logging
import datetime
import tempfile
import argparse
import threading

import requests
import pyaudio
import webrtcvad

from config import (
    OPENAI_API_KEY,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    LOCATION_NAME,
    BUSINESS_TYPE,
    WHISPER_LANGUAGE,
    VAD_AGGRESSIVENESS,
    PHRASES_COMMON,
    PHRASES_NEGATIVE,
    BUSINESS_PHRASES,
    PHRASES_CUSTOM,
)
from database import Database

# ── Логирование ──────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
os.makedirs("fails", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/monitor.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


class TrustControlMonitor:
    # Аудио параметры
    RATE = 16000
    CHANNELS = 1
    FRAME_MS = 30                              # длина фрейма VAD (мс)
    FRAME_SIZE = int(RATE * FRAME_MS / 1000)  # сэмплов в фрейме
    PRE_BUFFER_FRAMES = 10                     # фреймов предзаписи
    MIN_VOICED_FRAMES = 10                     # минимум для отправки
    MAX_SILENCE_FRAMES = 30                    # тишина = конец фразы

    def __init__(self):
        from openai import OpenAI
        self.openai = OpenAI(api_key=OPENAI_API_KEY)
        self.vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)
        self.db = Database()
        self.pa = pyaudio.PyAudio()
        self.audio_queue = queue.Queue()
        self.running = True

    # ── Анализ транскрипции ───────────────────────────────────

    def analyze(self, text: str) -> dict:
        result = {
            "greetings": [],
            "thanks": [],
            "farewells": [],
            "upsells": [],
            "rudeness": [],
            "fraud": [],
            "tone": "нейтральный 😐",
            "alerts": [],
        }
        t = text.lower()
        biz = BUSINESS_PHRASES.get(BUSINESS_TYPE, BUSINESS_PHRASES["shop"])

        def match(patterns: list, key: str):
            for p in patterns:
                if re.search(p, t):
                    result[key].append(p)

        match(PHRASES_COMMON["greetings"] + biz.get("greetings", []), "greetings")
        match(PHRASES_COMMON["thanks"], "thanks")
        match(PHRASES_COMMON["farewells"], "farewells")
        match(biz.get("upsells", []) + PHRASES_CUSTOM, "upsells")
        match(PHRASES_NEGATIVE["rudeness"], "rudeness")
        match(PHRASES_NEGATIVE["fraud"], "fraud")

        if result["rudeness"]:
            result["alerts"].append("ГРУБОСТЬ")
        if result["fraud"]:
            result["alerts"].append("МОШЕННИЧЕСТВО")

        positive = len(result["greetings"]) + len(result["thanks"])
        if result["rudeness"]:
            result["tone"] = "раздражённый 😤"
        elif positive >= 2:
            result["tone"] = "доброжелательный 😊"

        return result

    # ── Форматирование сообщений ──────────────────────────────

    def fmt_report(self, text: str, a: dict, ts: str) -> str:
        lines = [
            f"🏪 {LOCATION_NAME}  |  {ts}",
            "",
            "📝 Транскрипция:",
            text,
            "",
            "🔍 Обнаружено:",
        ]

        if a["greetings"]:
            lines.append(f"  ✅ Приветствие: `{a['greetings'][0]}`")
        else:
            lines.append("  ❌ Приветствие: отсутствует")

        if a["thanks"]:
            lines.append(f"  ✅ Благодарность: `{a['thanks'][0]}`")
        if a["farewells"]:
            lines.append(f"  ✅ Прощание: `{a['farewells'][0]}`")
        if a["upsells"]:
            lines.append(f"  ⭐ Допродажа: `{a['upsells'][0]}`")
        for r in a["rudeness"]:
            lines.append(f"  ⚠️ Грубость: `{r}`")
        for f in a["fraud"]:
            lines.append(f"  🚨 Мошенничество: `{f}`")

        lines += ["", f"Тон сотрудника: {a['tone']}"]
        return "\n".join(lines)

    def fmt_alert(self, alert_type: str, text: str, a: dict, ts: str) -> str:
        if alert_type == "ГРУБОСТЬ":
            header = "🔴 НАРУШЕНИЕ: ГРУБОСТЬ НА КАССЕ"
            phrases = a["rudeness"]
            footer = ""
        else:
            header = "🚨🚨🚨 СРОЧНО! ЛЕВАК НА КАССЕ! 🚨🚨🚨"
            phrases = a["fraud"]
            footer = "\nСотрудник пытается принять оплату мимо кассы!\nНемедленно проверьте!"

        lines = [
            header, "",
            f"📍 {LOCATION_NAME}",
            f"🕐 {ts}", "",
            "📝 Транскрипция:",
            text,
        ]
        for p in phrases:
            lines.append(f"\n🚨 {alert_type}: `{p}`")
        if footer:
            lines.append(footer)
        return "\n".join(lines)

    # ── Telegram ──────────────────────────────────────────────

    def tg_send(self, message: str) -> bool:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        try:
            r = requests.post(
                url,
                json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
                timeout=10,
            )
            if r.status_code == 200:
                logger.info("Telegram: отправлено")
                return True
            logger.error(f"Telegram error {r.status_code}: {r.text}")
        except Exception as e:
            logger.error(f"Telegram недоступен: {e}")

        self._save_offline(message)
        return False

    def _save_offline(self, message: str):
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = f"fails/report_{ts}.txt"
        with open(path, "w", encoding="utf-8") as f:
            f.write(message)
        logger.info(f"Сохранено офлайн: {path}")

    def _flush_offline(self):
        for fn in os.listdir("fails"):
            if not fn.endswith(".txt"):
                continue
            path = os.path.join("fails", fn)
            with open(path, encoding="utf-8") as f:
                msg = f.read()
            if self.tg_send(msg):
                os.remove(path)
                logger.info(f"Офлайн-отчёт отправлен: {fn}")

    # ── Транскрипция ──────────────────────────────────────────

    def transcribe(self, audio_bytes: bytes) -> str:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            with wave.open(tmp_path, "wb") as wf:
                wf.setnchannels(self.CHANNELS)
                wf.setsampwidth(2)
                wf.setframerate(self.RATE)
                wf.writeframes(audio_bytes)

            with open(tmp_path, "rb") as af:
                result = self.openai.audio.transcriptions.create(
                    model="whisper-1",
                    file=af,
                    language=WHISPER_LANGUAGE,
                )
            return result.text
        except Exception as e:
            logger.error(f"Ошибка Whisper: {e}")
            return ""
        finally:
            os.unlink(tmp_path)

    # ── Поток записи ─────────────────────────────────────────

    def _recorder(self):
        stream = self.pa.open(
            format=pyaudio.paInt16,
            channels=self.CHANNELS,
            rate=self.RATE,
            input=True,
            frames_per_buffer=self.FRAME_SIZE,
        )
        logger.info(f"Мониторинг запущен [{LOCATION_NAME}]. Слушаю...")
        print(f"\n Мониторинг запущен [{LOCATION_NAME}]. Слушаю...\n")

        pre_buf = []          # скользящий предбуфер
        voiced = []           # текущая запись
        silence = 0
        recording = False

        try:
            while self.running:
                frame = stream.read(self.FRAME_SIZE, exception_on_overflow=False)
                is_speech = self.vad.is_speech(frame, self.RATE)

                if is_speech:
                    if not recording:
                        recording = True
                        voiced = list(pre_buf)  # захватываем предбуфер
                    voiced.append(frame)
                    silence = 0
                else:
                    if recording:
                        voiced.append(frame)
                        silence += 1
                        if silence > self.MAX_SILENCE_FRAMES:
                            if len(voiced) > self.MIN_VOICED_FRAMES:
                                self.audio_queue.put(b"".join(voiced))
                            recording = False
                            voiced = []
                            silence = 0

                pre_buf.append(frame)
                if len(pre_buf) > self.PRE_BUFFER_FRAMES:
                    pre_buf.pop(0)

        except Exception as e:
            logger.error(f"Ошибка записи: {e}")
        finally:
            stream.stop_stream()
            stream.close()

    # ── Поток обработки ──────────────────────────────────────

    def _processor(self):
        while self.running:
            try:
                audio = self.audio_queue.get(timeout=1)
            except queue.Empty:
                continue

            try:
                logger.info("Транскрибирую...")
                text = self.transcribe(audio)

                if not text or len(text.strip()) < 5:
                    continue

                logger.info(f"Текст: {text}")
                ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                analysis = self.analyze(text)
                self.db.save_conversation(text, analysis, ts)

                # Тревоги — отправляем немедленно
                for alert in analysis["alerts"]:
                    self.tg_send(self.fmt_alert(alert, text, analysis, ts))

                # Обычный отчёт — только если нет тревог
                if not analysis["alerts"]:
                    self.tg_send(self.fmt_report(text, analysis, ts))

                self._flush_offline()

            except Exception as e:
                logger.error(f"Ошибка обработки: {e}")

    # ── Запуск ───────────────────────────────────────────────

    def run(self):
        rec = threading.Thread(target=self._recorder, daemon=True)
        proc = threading.Thread(target=self._processor, daemon=True)
        rec.start()
        proc.start()

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Остановка по Ctrl+C")
            self.running = False
        finally:
            self.pa.terminate()


# ── Точка входа ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="TrustControl Monitor")
    parser.add_argument("--config", help="Путь к config.py для данной точки")
    args = parser.parse_args()

    if args.config:
        import importlib.util
        spec = importlib.util.spec_from_file_location("config", args.config)
        cfg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cfg)
        sys.modules["config"] = cfg

    # Автоперезапуск при падении
    while True:
        try:
            TrustControlMonitor().run()
        except Exception as e:
            logger.error(f"Критическая ошибка: {e}. Перезапуск через 5 секунд...")
            time.sleep(5)


if __name__ == "__main__":
    main()
