"""Data and matching logic for the Sloboda leasing demo."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SOURCE_DATE = "2026-10-01"
DEMO_CANDIDATES = [
    ("Семейная студия «Маяк»", "Детское развитие", 180, 260, 2),
    ("Танцевальная студия «Ритм»", "Спорт и занятия", 220, 380, 2),
    ("Сервис «Дом рядом»", "Бытовые услуги", 100, 180, 2),
    ("Школа языков «Диалог»", "Образование", 180, 260, 2),
    ("Клиника «Пульс»", "Медицинские услуги", 200, 300, 2),
    ("Ателье «Нить»", "Бытовые услуги", 90, 150, 2),
    ("Мастерская «Глина»", "Творческий досуг", 180, 250, 2),
    ("Кафе «Соседи»", "Кафе", 300, 400, 2),
    ("Сервис печати «Точка»", "Бытовые услуги", 100, 140, 2),
    ("Семейный фитнес «Баланс»", "Спорт и занятия", 300, 420, 2),
]


def connect(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def initialize(db: sqlite3.Connection, source: Path) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS units (
            id TEXT PRIMARY KEY, floor INTEGER NOT NULL, area INTEGER NOT NULL,
            available INTEGER NOT NULL DEFAULT 1, asking_rent INTEGER,
            source_date TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tenants (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
            floor INTEGER NOT NULL, area INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            category TEXT NOT NULL, min_area INTEGER NOT NULL,
            max_area INTEGER NOT NULL, floor_pref INTEGER,
            budget INTEGER, contact TEXT, demo INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS matches (
            unit_id TEXT NOT NULL REFERENCES units(id),
            candidate_id INTEGER NOT NULL REFERENCES candidates(id),
            status TEXT NOT NULL DEFAULT 'предложен', reason TEXT DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (unit_id, candidate_id)
        );
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            unit_id TEXT NOT NULL REFERENCES units(id),
            candidate_id INTEGER NOT NULL REFERENCES candidates(id),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS general_inquiries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL REFERENCES candidates(id),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            role TEXT NOT NULL CHECK (role IN ('manager', 'tenant')),
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS conversation_state (
            telegram_id INTEGER PRIMARY KEY,
            session_json TEXT,
            context_json TEXT
        );
        CREATE TABLE IF NOT EXISTS runtime_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    section = source.read_text(encoding="utf-8").split("# tenants_slobodka.xlsx", 1)[1].split("# tenants_meridian.xlsx", 1)[0]
    found = []
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 5 or not cells[0].startswith("С-"):
            continue
        unit_id, floor, name, category, area = cells[:5]
        found.append((unit_id, int(floor), name, category, int(area)))
    vacant = [row for row in found if row[2] == "свободно"]
    if len(vacant) != 6 or sum(row[4] for row in vacant) != 1392:
        raise ValueError("Исходные данные «Слободы» изменились: проверьте импорт")
    with db:
        for unit_id, floor, name, category, area in found:
            if name == "свободно":
                db.execute("INSERT OR IGNORE INTO units VALUES (?, ?, ?, 1, NULL, ?)", (unit_id, floor, area, SOURCE_DATE))
            else:
                db.execute("INSERT OR IGNORE INTO tenants VALUES (?, ?, ?, ?, ?)", (unit_id, name, category, floor, area))
        if db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0:
            db.executemany("INSERT INTO candidates (name, category, min_area, max_area, floor_pref, demo) VALUES (?, ?, ?, ?, ?, 1)", DEMO_CANDIDATES)


def available_units(db):
    return db.execute("SELECT * FROM units WHERE available = 1 ORDER BY id").fetchall()


def unit(db, unit_id):
    return db.execute("SELECT * FROM units WHERE id = ?", (unit_id,)).fetchone()


def candidate(db, candidate_id):
    return db.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()


def shortlist(db, unit_id: str, limit: int = 3):
    space = unit(db, unit_id)
    if space is None or not space["available"]:
        return []
    counts = {row[0].casefold(): row[1] for row in db.execute("SELECT category, COUNT(*) FROM tenants GROUP BY category")}
    rows = db.execute("SELECT * FROM candidates WHERE min_area <= ? AND max_area >= ? AND (floor_pref IS NULL OR floor_pref = ?)", (space["area"], space["area"], space["floor"])).fetchall()
    ranked = []
    for item in rows:
        if space["asking_rent"] is not None and item["budget"] is not None and item["budget"] < space["asking_rent"]:
            continue
        midpoint = (item["min_area"] + item["max_area"]) / 2
        area_score = 50 * (1 - abs(space["area"] - midpoint) / max(1, (item["max_area"] - item["min_area"]) / 2))
        scarcity = max(0, 30 - 10 * counts.get(item["category"].casefold(), 0))
        future_family_signal = 10 if item["category"] in {"Детское развитие", "Образование", "Спорт и занятия"} else 0
        score = round(area_score + scarcity + future_family_signal, 1)
        ranked.append((score, item))
    ranked.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
    return ranked[:limit]


def add_candidate(db, name, category, min_area, max_area, floor_pref=None, budget=None, contact=None):
    name, category = name.strip(), category.strip()
    if not name or not category or len(name) > 120 or len(category) > 80:
        raise ValueError("Укажите название и категорию (до 120 и 80 символов)")
    if not 1 <= min_area <= max_area <= 100000:
        raise ValueError("Проверьте диапазон площади")
    if floor_pref not in (None, 1, 2):
        raise ValueError("Этаж: 1, 2 или любой")
    if budget is not None and not 1 <= budget <= 1000000:
        raise ValueError("Проверьте бюджет")
    with db:
        return db.execute("INSERT INTO candidates (name, category, min_area, max_area, floor_pref, budget, contact) VALUES (?, ?, ?, ?, ?, ?, ?)", (name, category, min_area, max_area, floor_pref, budget, (contact or "").strip()[:200])).lastrowid


STATUSES = {"предложен", "одобрен", "связались", "интерес", "просмотр", "отказ"}


def set_status(db, unit_id, candidate_id, status, reason=""):
    if status not in STATUSES or unit(db, unit_id) is None or candidate(db, candidate_id) is None:
        raise ValueError("Неизвестный статус, помещение или кандидат")
    with db:
        db.execute("""INSERT INTO matches (unit_id, candidate_id, status, reason)
            VALUES (?, ?, ?, ?) ON CONFLICT(unit_id, candidate_id) DO UPDATE SET
            status=excluded.status, reason=excluded.reason, updated_at=CURRENT_TIMESTAMP""",
            (unit_id, candidate_id, status, reason[:300]))


def stats(db):
    return db.execute("SELECT status, COUNT(*) FROM matches GROUP BY status ORDER BY status").fetchall()


def create_application(db, unit_id, telegram_id, name, category, min_area, max_area, contact=""):
    space = unit(db, unit_id)
    if space is None or not space["available"]:
        raise ValueError("Помещение уже недоступно")
    actual_contact = (contact or f"Telegram ID: {telegram_id}").strip()
    with db:
        candidate_id = add_candidate(db, name, category, min_area, max_area, space["floor"], contact=actual_contact)
        db.execute("INSERT INTO applications (unit_id, candidate_id) VALUES (?, ?)", (unit_id, candidate_id))
        return candidate_id


def applications(db):
    return db.execute("""SELECT 'P-' || a.id AS id, a.unit_id, a.created_at, c.id AS candidate_id,
        c.name, c.category, c.min_area, c.max_area, c.contact
        FROM applications a JOIN candidates c ON c.id = a.candidate_id
        UNION ALL
        SELECT 'G-' || g.id AS id, NULL AS unit_id, g.created_at, c.id AS candidate_id,
        c.name, c.category, c.min_area, c.max_area, c.contact
        FROM general_inquiries g JOIN candidates c ON c.id = g.candidate_id
        ORDER BY created_at DESC, id DESC""").fetchall()


def create_general_inquiry(db, telegram_id, name, category, min_area, max_area, contact=""):
    actual_contact = (contact or f"Telegram ID: {telegram_id}").strip()
    with db:
        candidate_id = add_candidate(db, name, category, min_area, max_area, contact=actual_contact)
        db.execute("INSERT INTO general_inquiries (candidate_id) VALUES (?)", (candidate_id,))
        return candidate_id


def user_role(db, telegram_id):
    row = db.execute("SELECT role FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
    return row["role"] if row else None


def set_user_role(db, telegram_id, role):
    if role not in {"manager", "tenant"}:
        raise ValueError("Неизвестная роль")
    with db:
        db.execute("""INSERT INTO users (telegram_id, role) VALUES (?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET role=excluded.role, updated_at=CURRENT_TIMESTAMP""",
            (telegram_id, role))


def load_conversations(db):
    sessions, contexts = {}, {}
    for row in db.execute("SELECT telegram_id, session_json, context_json FROM conversation_state"):
        try:
            if row["session_json"]:
                session = json.loads(row["session_json"])
                if isinstance(session, list) and len(session) == 2 and isinstance(session[0], str) and isinstance(session[1], dict):
                    sessions[row["telegram_id"]] = (session[0], session[1])
            if row["context_json"]:
                context = json.loads(row["context_json"])
                if isinstance(context, dict):
                    contexts[row["telegram_id"]] = context
        except (ValueError, TypeError):
            continue
    return sessions, contexts


def save_conversation(db, telegram_id, session, context):
    with db:
        if session is None and not context:
            db.execute("DELETE FROM conversation_state WHERE telegram_id = ?", (telegram_id,))
        else:
            db.execute("""INSERT INTO conversation_state (telegram_id, session_json, context_json) VALUES (?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET session_json=excluded.session_json, context_json=excluded.context_json""",
                (telegram_id, json.dumps(session, ensure_ascii=False) if session is not None else None,
                 json.dumps(context, ensure_ascii=False) if context else None))


def poll_offset(db):
    row = db.execute("SELECT value FROM runtime_meta WHERE key = 'poll_offset'").fetchone()
    return int(row["value"]) if row else 0


def save_poll_offset(db, offset):
    with db:
        db.execute("""INSERT INTO runtime_meta (key, value) VALUES ('poll_offset', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (str(offset),))
