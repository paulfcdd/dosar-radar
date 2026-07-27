"""Синхронізація: оновлення списку наказів і розбір PDF."""

import fcntl
import logging
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta

from . import db
from .scraper import INDEX_URL, parse_index, parse_pdf
from .session import Session

log = logging.getLogger(__name__)

LAST_SYNC_KEY = "last_sync_at"
LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(db.DB_PATH)), "sync.lock")

# pdfplumber голосно скаржиться на нестандартні кольори у цих PDF — це не помилки.
logging.getLogger("pdfminer").setLevel(logging.ERROR)
logging.getLogger("pdfplumber").setLevel(logging.ERROR)

_session = Session()

# Стан фонової синхронізації для веб-інтерфейсу.
_state = {"running": False, "done": 0, "total": 0, "message": "не запускалась"}
_state_lock = threading.Lock()


class SyncBusy(RuntimeError):
    """Синхронізація вже йде — в цьому або в іншому процесі."""


@contextmanager
def exclusive():
    """Блокує паралельні синхронізації між процесами.

    Прапорця `_state["running"]` мало: він живе в пам'яті одного процесу, а
    синк запускають і планувальник у вебі, і `make sync` в окремому контейнері.
    flock на файлі в томі бачать обидва й він сам звільняється, якщо процес помер.
    """
    handle = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(handle)
        raise SyncBusy("синхронізація вже виконується")
    try:
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)


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


def run(workers=4, limit=None, refresh=True, ignore_backoff=False):
    """Повна синхронізація: індекс + усі нерозібрані накази."""
    with exclusive():
        if refresh:
            _set(running=True, message="оновлюю список наказів…", done=0, total=0)
            total, added = refresh_index()
            log.info("на сторінці %d наказів, нових %d", total, added)

        orders = db.pending_orders(limit, ignore_backoff=ignore_backoff)
        _set(running=True, total=len(orders), done=0, message="розбираю PDF…")

        if orders:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for _ in pool.map(parse_order, orders):
                    with _state_lock:
                        _state["done"] += 1

        db.set_meta(LAST_SYNC_KEY, db.utcnow().isoformat())
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
    except SyncBusy:
        log.info("синхронізація вже йде — пропускаю")
    except Exception as exc:  # noqa: BLE001
        log.exception("синхронізація впала")
        _set(running=False, message="помилка: %s" % exc)


def interval_hours():
    return float(os.environ.get("SYNC_INTERVAL_HOURS", "3"))


def schedule():
    """Коли синхронізували востаннє й коли планується наступна."""
    last = db.get_meta(LAST_SYNC_KEY)
    if not last:
        return {"last": None, "next": None}
    try:
        last_at = datetime.fromisoformat(last)
    except ValueError:
        return {"last": None, "next": None}
    return {
        "last": last_at.isoformat(sep=" "),
        "next": (last_at + timedelta(hours=interval_hours())).isoformat(sep=" "),
    }


def _seconds_until_due():
    last = db.get_meta(LAST_SYNC_KEY)
    if not last:
        return 0
    try:
        due = datetime.fromisoformat(last) + timedelta(hours=interval_hours())
    except ValueError:
        return 0
    return max(0, (due - db.utcnow()).total_seconds())


def _scheduler_loop():
    while True:
        wait = _seconds_until_due()
        if wait > 0:
            # Прокидаємось не рідше ніж раз на хвилину, щоб зміна інтервалу
            # чи ручний синк не залишили нас спати на години.
            time.sleep(min(wait, 60))
            continue
        # Джитер, щоб не стукати рівно о цілій годині разом з усіма.
        time.sleep(random.uniform(0, 120))
        if _seconds_until_due() > 0:
            continue
        log.info("плановий синк")
        _run_guarded()


def start_scheduler():
    """Запускає фонове автооновлення. Викликати лише з веб-процесу."""
    if os.environ.get("SYNC_ENABLED", "1").lower() not in ("1", "true", "yes", "on"):
        log.info("автосинк вимкнено через SYNC_ENABLED")
        return False
    threading.Thread(target=_scheduler_loop, daemon=True).start()
    log.info("автосинк кожні %g год", interval_hours())
    return True
