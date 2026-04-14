#!/usr/bin/env python3
"""
TrustControl — База данных (история, статистика)
"""

import sqlite3
import json
import datetime
import sys


class Database:
    def __init__(self, db_path="trustcontrol.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp   TEXT NOT NULL,
                    location    TEXT,
                    transcript  TEXT,
                    greetings   TEXT,
                    thanks      TEXT,
                    farewells   TEXT,
                    upsells     TEXT,
                    rudeness    TEXT,
                    fraud       TEXT,
                    tone        TEXT,
                    alerts      TEXT,
                    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp   TEXT NOT NULL,
                    location    TEXT,
                    alert_type  TEXT,
                    transcript  TEXT,
                    phrases     TEXT,
                    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    def save_conversation(self, transcript, analysis, timestamp, location=None):
        try:
            from config import LOCATION_NAME
            loc = location or LOCATION_NAME
        except ImportError:
            loc = location or "Неизвестно"

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO conversations
                    (timestamp, location, transcript, greetings, thanks, farewells,
                     upsells, rudeness, fraud, tone, alerts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    loc,
                    transcript,
                    json.dumps(analysis.get("greetings", []), ensure_ascii=False),
                    json.dumps(analysis.get("thanks", []), ensure_ascii=False),
                    json.dumps(analysis.get("farewells", []), ensure_ascii=False),
                    json.dumps(analysis.get("upsells", []), ensure_ascii=False),
                    json.dumps(analysis.get("rudeness", []), ensure_ascii=False),
                    json.dumps(analysis.get("fraud", []), ensure_ascii=False),
                    analysis.get("tone", "нейтральный"),
                    json.dumps(analysis.get("alerts", []), ensure_ascii=False),
                ),
            )

            for alert_type in analysis.get("alerts", []):
                if alert_type == "ГРУБОСТЬ":
                    phrases = analysis.get("rudeness", [])
                elif alert_type == "МОШЕННИЧЕСТВО":
                    phrases = analysis.get("fraud", [])
                else:
                    phrases = []

                conn.execute(
                    """
                    INSERT INTO alerts (timestamp, location, alert_type, transcript, phrases)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (timestamp, loc, alert_type, transcript,
                     json.dumps(phrases, ensure_ascii=False)),
                )

            conn.commit()

    def get_recent_alerts(self, limit=10):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT * FROM alerts ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()

    def get_stats(self, days=7):
        since = (
            datetime.datetime.now() - datetime.timedelta(days=days)
        ).strftime("%Y-%m-%d")

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row

            def count(sql, params=()):
                return conn.execute(sql, params).fetchone()[0]

            total = count(
                "SELECT COUNT(*) FROM conversations WHERE timestamp >= ?", (since,)
            )
            with_greeting = count(
                "SELECT COUNT(*) FROM conversations WHERE timestamp >= ? AND greetings != '[]'",
                (since,),
            )
            with_thanks = count(
                "SELECT COUNT(*) FROM conversations WHERE timestamp >= ? AND thanks != '[]'",
                (since,),
            )
            with_upsell = count(
                "SELECT COUNT(*) FROM conversations WHERE timestamp >= ? AND upsells != '[]'",
                (since,),
            )
            rudeness_count = count(
                "SELECT COUNT(*) FROM alerts WHERE timestamp >= ? AND alert_type = 'ГРУБОСТЬ'",
                (since,),
            )
            fraud_count = count(
                "SELECT COUNT(*) FROM alerts WHERE timestamp >= ? AND alert_type = 'МОШЕННИЧЕСТВО'",
                (since,),
            )
            tones = conn.execute(
                "SELECT tone, COUNT(*) as cnt FROM conversations "
                "WHERE timestamp >= ? GROUP BY tone ORDER BY cnt DESC",
                (since,),
            ).fetchall()

        def pct(part, total):
            return round(part / total * 100, 1) if total > 0 else 0

        return {
            "period_days": days,
            "total": total,
            "with_greeting": with_greeting,
            "with_thanks": with_thanks,
            "with_upsell": with_upsell,
            "rudeness_alerts": rudeness_count,
            "fraud_alerts": fraud_count,
            "tones": [(r["tone"], r["cnt"]) for r in tones],
            "greeting_rate": pct(with_greeting, total),
            "thanks_rate": pct(with_thanks, total),
            "upsell_rate": pct(with_upsell, total),
        }

    def print_stats(self, days=7):
        s = self.get_stats(days)
        print(f"\n{'=' * 50}")
        print(f"  TrustControl — Статистика за {s['period_days']} дней")
        print(f"{'=' * 50}")
        print(f"  Всего разговоров:        {s['total']}")
        print(f"  Приветствий:             {s['with_greeting']} ({s['greeting_rate']}%)")
        print(f"  Благодарностей:          {s['with_thanks']} ({s['thanks_rate']}%)")
        print(f"  Допродаж:                {s['with_upsell']} ({s['upsell_rate']}%)")
        print(f"  Тревог (грубость):       {s['rudeness_alerts']}")
        print(f"  Тревог (мошенничество):  {s['fraud_alerts']}")
        if s["tones"]:
            print(f"\n  Тон сотрудников:")
            for tone, cnt in s["tones"]:
                print(f"    {tone}: {cnt}")
        print(f"{'=' * 50}\n")


if __name__ == "__main__":
    db = Database()
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        db.print_stats(days)
    else:
        print("Использование: python database.py stats [дней]")
        print("Пример:        python database.py stats 30")
