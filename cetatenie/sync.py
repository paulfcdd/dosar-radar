"""Синхронізація: оновлення списку наказів і розбір PDF."""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from . import db
from .scraper import INDEX_URL, parse_index, parse_pdf
from .session import Session

log = logging.getLogger(__name__)

# pdfplumber голосно скаржиться на нестандартні кольори у цих PDF — це не помилки.
logging.getLogger("pdfminer").setLevel(logging.ERROR)
logging.getLogger("pdfplumber").setLevel(logging.ERROR)

_session = Session()

# Стан фонової синхронізації для веб-інтерфейсу.
_state = {"running": False, "done": 0, "total": 0, "message": "не запускалась"}
_state_lock = threading.Lock()


def state():
    with _state_lock:
        return dict(_state)


def _set(**kwargs):
    with _state_lock:
        _state.update(kwargs)


def refresh_index():
    """Перечитує сторінку зі списком наказів. Повертає (всього, нових)."""
    response = _session.get(INDEX_URL)
    response.raise_for_status()
    orders = parse_index(response.text)
    if not orders:
        raise RuntimeError("на сторінці не знайдено жодного наказу")
    return len(orders), db.upsert_orders(orders)


def parse_order(order):
    """Качає PDF наказу і зберігає знайдені номери справ."""
    try:
        response = _session.get(order["pdf_url"])
        response.raise_for_status()
        if "pdf" not in response.headers.get("Content-Type", ""):
            raise RuntimeError("очікувався PDF, отримано %s" % response.headers.get("Content-Type"))
        cases, text = parse_pdf(response.content)
        if not cases and len(text.strip()) < 50:
            raise RuntimeError("у PDF немає текстового шару (ймовірно скан)")
        db.save_cases(order["id"], cases)
        return len(cases)
    except Exception as exc:  # noqa: BLE001 — помилку показуємо в інтерфейсі
        log.warning("не вдалось розібрати %s: %s", order["pdf_url"], exc)
        db.mark_error(order["id"], exc)
        return 0


def run(workers=4, limit=None, refresh=True):
    """Повна синхронізація: індекс + усі нерозібрані накази."""
    if refresh:
        _set(running=True, message="оновлюю список наказів…", done=0, total=0)
        total, added = refresh_index()
        log.info("на сторінці %d наказів, нових %d", total, added)

    orders = db.pending_orders(limit)
    _set(running=True, total=len(orders), done=0, message="розбираю PDF…")

    if orders:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for _ in pool.map(parse_order, orders):
                with _state_lock:
                    _state["done"] += 1

    _set(running=False, message="готово")
    return len(orders)


def run_background(**kwargs):
    if state()["running"]:
        return False
    thread = threading.Thread(target=_run_guarded, kwargs=kwargs, daemon=True)
    thread.start()
    return True


def _run_guarded(**kwargs):
    try:
        run(**kwargs)
    except Exception as exc:  # noqa: BLE001
        log.exception("синхронізація впала")
        _set(running=False, message="помилка: %s" % exc)
