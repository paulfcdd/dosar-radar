"""Сховище на SQLite: накази та номери справ з них."""

import os
import sqlite3
from datetime import datetime

DB_PATH = os.environ.get(
    "CETATENIE_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data.sqlite3"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY,
    number      TEXT,
    date        TEXT NOT NULL,          -- ДД.ММ.РРРР як на сайті
    date_iso    TEXT NOT NULL,          -- РРРР-ММ-ДД для сортування/фільтрів
    year        INTEGER NOT NULL,
    pdf_url     TEXT NOT NULL UNIQUE,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | parsed | error
    error       TEXT,
    case_count  INTEGER NOT NULL DEFAULT 0,
    parsed_at   TEXT
);
CREATE TABLE IF NOT EXISTS cases (
    id          INTEGER PRIMARY KEY,
    order_id    INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    case_number TEXT NOT NULL,          -- як надруковано, напр. 48031/RD/2023
    search_key  TEXT NOT NULL,          -- уніфіковано «номер/рік», напр. 48031/2023
    case_year   INTEGER NOT NULL,
    minors      INTEGER NOT NULL DEFAULT 0,
    raw         TEXT,
    UNIQUE(order_id, case_number)
);
CREATE INDEX IF NOT EXISTS idx_cases_number ON cases(case_number);
CREATE INDEX IF NOT EXISTS idx_cases_key ON cases(search_key);
CREATE INDEX IF NOT EXISTS idx_orders_date ON orders(date_iso);
"""


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init():
    with connect() as conn:
        conn.executescript(SCHEMA)


def to_iso(date):
    day, month, year = date.split(".")
    return "%s-%s-%s" % (year, month, day)


def upsert_orders(orders):
    """Додає нові накази. Повертає кількість доданих."""
    added = 0
    with connect() as conn:
        for order in orders:
            cursor = conn.execute(
                """INSERT INTO orders (number, date, date_iso, year, pdf_url)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(pdf_url) DO NOTHING""",
                (
                    order["number"],
                    order["date"],
                    to_iso(order["date"]),
                    order["year"],
                    order["pdf_url"],
                ),
            )
            added += cursor.rowcount
    return added


def save_cases(order_id, cases):
    with connect() as conn:
        conn.execute("DELETE FROM cases WHERE order_id = ?", (order_id,))
        conn.executemany(
            """INSERT OR IGNORE INTO cases
                   (order_id, case_number, search_key, case_year, minors, raw)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (order_id, c["case_number"], c["search_key"], c["case_year"], c["minors"], c["raw"])
                for c in cases
            ],
        )
        conn.execute(
            """UPDATE orders SET status = 'parsed', error = NULL,
                                 case_count = ?, parsed_at = ? WHERE id = ?""",
            (len(cases), datetime.utcnow().isoformat(timespec="seconds"), order_id),
        )


def mark_error(order_id, message):
    with connect() as conn:
        conn.execute(
            "UPDATE orders SET status = 'error', error = ? WHERE id = ?",
            (str(message)[:500], order_id),
        )


def pending_orders(limit=None):
    query = "SELECT * FROM orders WHERE status != 'parsed' ORDER BY date_iso DESC"
    if limit:
        query += " LIMIT %d" % int(limit)
    with connect() as conn:
        return [dict(row) for row in conn.execute(query)]


def stats():
    with connect() as conn:
        row = conn.execute(
            """SELECT (SELECT COUNT(*) FROM orders) AS orders,
                      (SELECT COUNT(*) FROM orders WHERE status = 'parsed') AS parsed,
                      (SELECT COUNT(*) FROM orders WHERE status = 'error') AS errors,
                      (SELECT COUNT(*) FROM cases) AS cases"""
        ).fetchone()
        years = [
            dict(r)
            for r in conn.execute(
                """SELECT year, COUNT(*) AS orders,
                          COALESCE(SUM(case_count), 0) AS cases
                   FROM orders GROUP BY year ORDER BY year DESC"""
            )
        ]
    return dict(row), years


def list_orders(year=None, date_from=None, date_to=None):
    where, params = [], []
    if year:
        where.append("year = ?")
        params.append(int(year))
    if date_from:
        where.append("date_iso >= ?")
        params.append(date_from)
    if date_to:
        where.append("date_iso <= ?")
        params.append(date_to)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM orders %s ORDER BY date_iso DESC, number" % clause, params
            )
        ]


def get_order(order_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            return None, []
        cases = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM cases WHERE order_id = ? ORDER BY id", (order_id,)
            )
        ]
    return dict(row), cases


def search_case(query):
    """Пошук справи за '32805/2023', '32805/RD/2023' або просто '32805'."""
    query = query.strip().strip("()").replace(" ", "")
    if "/" in query:
        # Зводимо до «номер/рік», щоб запит 48031/2023 знайшов і 48031/RD/2023.
        parts = query.split("/")
        key = "%s/%s" % (parts[0], parts[-1])
        condition, params = "(c.search_key = ? OR c.case_number = ?)", [key, query]
    else:
        condition, params = "c.search_key LIKE ?", [query + "/%"]
    with connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                """SELECT c.case_number, c.minors, c.raw,
                          o.id AS order_id, o.number, o.date, o.date_iso, o.year, o.pdf_url
                   FROM cases c JOIN orders o ON o.id = c.order_id
                   WHERE %s ORDER BY o.date_iso DESC""" % condition,
                params,
            )
        ]
