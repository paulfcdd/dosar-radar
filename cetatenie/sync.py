"""Синхронізація: оновлення списку наказів і розбір PDF."""

import fcntl
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta

import requests

from . import db
from .scraper import INDEX_URL, parse_index, parse_pdf
from .session import Session

log = logging.getLogger(__name__)

LAST_SYNC_KEY = "last_sync_at"
LAST_ATTEMPT_KEY = "last_sync_attempt_at"
LAST_FAILURE_KEY = "last_sync_failure_at"
LAST_ERROR_KEY = "last_sync_error"
FAILURE_COUNT_KEY = "sync_failure_count"
NEXT_SYNC_KEY = "next_sync_at"
LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(db.DB_PATH)), "sync.lock")

# pdfplumber голосно скаржиться на нестандартні кольори у цих PDF — це не помилки.
logging.getLogger("pdfminer").setLevel(logging.ERROR)
logging.getLogger("pdfplumber").setLevel(logging.ERROR)

_session = Session()

# Стан фонової синхронізації для веб-інтерфейсу.
_state = {
    "running": False,
    "done": 0,
    "total": 0,
    "message": "не запускалась",
    "last_error": None,
}
_state_lock = threading.Lock()


class SyncBusy(RuntimeError):
    """Синхронізація вже йде — в цьому або в іншому процесі."""


class SourceAccessError(RuntimeError):
    """Джерело відхилило запит або недоступне для сервера."""


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
    if response.status_code == 403 and "WAF Forbidden" in response.text[:2000]:
        raise SourceAccessError(
            "WAF cetatenie.just.ro відхилив IP сервера; потрібен allowlist або дозволений proxy"
        )
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


def _meta_datetime(key):
    value = db.get_meta(key)
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _failure_count():
    try:
        return int(db.get_meta(FAILURE_COUNT_KEY, "0"))
    except ValueError:
        return 0


def public_error(error):
    """Коротке безпечне для публічного UI пояснення помилки синхронізації."""
    message = str(error)
    if isinstance(error, SourceAccessError):
        return message
    if isinstance(error, str):
        if not any(
            token in message
            for token in ("HTTPSConnectionPool", "ConnectionError", "ConnectTimeout", "Network is unreachable")
        ):
            return message
    if isinstance(error, requests.RequestException) or any(
        token in message for token in ("HTTPSConnectionPool", "ConnectionError", "ConnectTimeout", "Network is unreachable")
    ):
        return (
            "Сервер тимчасово не може підключитися до сайту міністерства. "
            "Автоматична спроба повториться за розкладом."
        )
    return "Не вдалося оновити дані з сайту міністерства. Автоматична спроба повториться за розкладом."


def retry_delay(count):
    """Експоненційна пауза після помилки, щоб не тиснути на джерело."""
    base_minutes = float(os.environ.get("SYNC_RETRY_MINUTES", "5"))
    max_minutes = float(os.environ.get("SYNC_RETRY_MAX_MINUTES", "360"))
    return timedelta(minutes=min(base_minutes * (2 ** max(0, count - 1)), max_minutes))


def _record_success():
    now = db.utcnow()
    db.set_meta(LAST_SYNC_KEY, now.isoformat())
    db.set_meta(NEXT_SYNC_KEY, (now + timedelta(hours=interval_hours())).isoformat())
    db.set_meta(FAILURE_COUNT_KEY, 0)
    db.set_meta(LAST_ERROR_KEY, "")
    _set(running=False, message="готово", last_error=None)


def _record_failure(exc):
    now = db.utcnow()
    count = _failure_count() + 1
    next_at = now + retry_delay(count)
    message = public_error(exc)[:500]
    db.set_meta(LAST_FAILURE_KEY, now.isoformat())
    db.set_meta(LAST_ERROR_KEY, message)
    db.set_meta(FAILURE_COUNT_KEY, count)
    db.set_meta(NEXT_SYNC_KEY, next_at.isoformat())
    _set(running=False, message="помилка синхронізації", last_error=message)
    return next_at


def run(workers=4, limit=None, refresh=True, ignore_backoff=False):
    """Повна синхронізація: індекс + усі нерозібрані накази."""
    with exclusive():
        attempt_at = db.utcnow()
        db.set_meta(LAST_ATTEMPT_KEY, attempt_at.isoformat())
        _set(running=True, message="оновлюю список наказів…", done=0, total=0, last_error=None)
        try:
            if refresh:
                total, added = refresh_index()
                log.info("на сторінці %d наказів, нових %d", total, added)

            orders = db.pending_orders(limit, ignore_backoff=ignore_backoff)
            _set(running=True, total=len(orders), done=0, message="розбираю PDF…")

            if orders:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    for _ in pool.map(parse_order, orders):
                        with _state_lock:
                            _state["done"] += 1
        except Exception as exc:
            next_at = _record_failure(exc)
            log.warning("синхронізація не виконалась; наступна спроба %s: %s", next_at, exc)
            raise

        _record_success()
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


def interval_hours():
    return float(os.environ.get("SYNC_INTERVAL_HOURS", "3"))


def schedule():
    """Показує останній успіх, помилку та фактичний час наступної спроби."""
    last_at = _meta_datetime(LAST_SYNC_KEY)
    next_at = _meta_datetime(NEXT_SYNC_KEY)
    if not next_at and last_at:
        next_at = last_at + timedelta(hours=interval_hours())
    return {
        "last": last_at.isoformat(sep=" ") if last_at else None,
        "last_attempt": (
            _meta_datetime(LAST_ATTEMPT_KEY).isoformat(sep=" ")
            if _meta_datetime(LAST_ATTEMPT_KEY)
            else None
        ),
        "last_failure": (
            _meta_datetime(LAST_FAILURE_KEY).isoformat(sep=" ")
            if _meta_datetime(LAST_FAILURE_KEY)
            else None
        ),
        "error": public_error(db.get_meta(LAST_ERROR_KEY)) if db.get_meta(LAST_ERROR_KEY) else None,
        "next": next_at.isoformat(sep=" ") if next_at else None,
    }


def _seconds_until_due():
    due = _meta_datetime(NEXT_SYNC_KEY)
    if not due:
        last = _meta_datetime(LAST_SYNC_KEY)
        due = last + timedelta(hours=interval_hours()) if last else None
    if not due:
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
