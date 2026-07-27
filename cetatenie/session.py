"""HTTP-сесія, яка вміє проходити JS-челендж сайту cetatenie.just.ro.

Сайт віддає 503 зі сторінкою-челенджем, де треба підібрати лічильник i такий,
що SHA1(c + str(i)) має байт 0xb0 на позиції n1 і 0x0b на n1+1, де c — токен зі
сторінки, а n1 — перший шістнадцятковий символ цього токена. Результат кладеться
в cookie `res=<c><i>`.
"""

import hashlib
import logging
import re
import socket
import threading

import requests
import urllib3.util.connection

log = logging.getLogger(__name__)

BASE_URL = "https://cetatenie.just.ro"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_ARRAY_RE = re.compile(r"a0_0x2a54\s*=\s*\[([^\]]*)\]")
_STRING_RE = re.compile(r"'([^']*)'")
_TOKEN_RE = re.compile(r"^[0-9A-Fa-f]{32,64}$")
_MAX_NONCE = 20_000_000


class ChallengeError(RuntimeError):
    pass


def _ipv6_is_routable():
    """Чи є справжній маршрут в IPv6, а не лише можливість створити сокет.

    UDP-`connect` не шле жодного пакета — лише просить ядро вибрати маршрут,
    тож перевірка миттєва й безкоштовна.
    """
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as probe:
            probe.settimeout(1)
            probe.connect(("2001:4860:4860::8888", 53))
        return True
    except OSError:
        return False


def prefer_ipv4_if_needed():
    """Вимикає IPv6 у urllib3, якщо маршруту насправді немає.

    У docker-мережі без IPv6 контейнер усе одно вміє створити AF_INET6-сокет,
    тому `urllib3.util.connection.HAS_IPV6` лишається True. cetatenie.just.ro
    має AAAA-запис, glibc віддає його першим — і кожне таке з'єднання падає з
    `[Errno 101] Network is unreachable`. Ефект спостерігався як ~70% невдалих
    завантажень PDF, тобто синк «майже працює», що гірше за явну поломку.
    """
    if urllib3.util.connection.HAS_IPV6 and not _ipv6_is_routable():
        urllib3.util.connection.HAS_IPV6 = False
        log.info("IPv6 без маршруту — ходимо лише через IPv4")


prefer_ipv4_if_needed()


def _looks_like_challenge(text):
    return "Enable JavaScript and cookies" in text or "a0_0x2a54" in text


def _roles(items):
    """Розкладає рядки челенджа за ролями.

    Масив у скрипті ротується на різну кількість кроків у кожній відповіді, тож
    покладатись на порядок не можна — ролі визначаємо за самим вмістом:
    40-символьний hex — це токен, рядок із '=' — префікс cookie.
    """
    token = next((s for s in items if _TOKEN_RE.match(s)), None)
    prefix = next((s for s in items if s.endswith("=") and s != token), None)
    if not token or not prefix:
        raise ChallengeError("не вдалось розпізнати челендж: %r" % (items,))
    return prefix, token


def solve_challenge(html):
    """Повертає (ім'я_cookie, значення_cookie) для сторінки-челенджа."""
    array_match = _ARRAY_RE.search(html)
    if not array_match:
        raise ChallengeError("не знайдено даних челенджа у відповіді")

    prefix, token = _roles(_STRING_RE.findall(array_match.group(1)))
    name = prefix.rstrip("=")
    offset = int(token[0], 16)

    for nonce in range(_MAX_NONCE):
        digest = hashlib.sha1((token + str(nonce)).encode()).digest()
        if digest[offset] == 0xB0 and digest[offset + 1] == 0x0B:
            return name, token + str(nonce)
    raise ChallengeError("розв'язок челенджа не знайдено")


class Session:
    """requests.Session, яка автоматично розв'язує челендж і кешує cookie."""

    def __init__(self, timeout=60):
        self.timeout = timeout
        self._lock = threading.Lock()
        self._session = requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT

    def _is_challenge(self, response):
        if "html" not in response.headers.get("Content-Type", ""):
            return False
        return response.status_code == 503 or _looks_like_challenge(response.text[:4000])

    def get(self, url, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        response = self._session.get(url, **kwargs)
        if not self._is_challenge(response):
            return response

        with self._lock:
            name, value = solve_challenge(response.text)
            self._session.cookies.set(name, value, domain="cetatenie.just.ro", path="/")
        return self._session.get(url, **kwargs)
