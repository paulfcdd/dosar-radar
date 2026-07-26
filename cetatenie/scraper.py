"""Парсинг сторінки зі списком наказів та самих PDF-файлів."""

import io
import logging
import re

import pdfplumber
from bs4 import BeautifulSoup

from .session import BASE_URL

log = logging.getLogger(__name__)

INDEX_URL = BASE_URL + "/ordine-articolul-1-1/"

_DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})")
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")
# Номер справи в PDF: (16545/2024) і рідший варіант (48031/RD/2023).
_CASE_RE = re.compile(r"\(?\b(\d{1,6})\s*/\s*(?:(RD)\s*/\s*)?((?:19|20)\d{2})\b\)?")
_MINORS_RE = re.compile(r"Copii\s+minori\s*:\s*(\d+)", re.IGNORECASE)
# Рядки шапки й підвалу, де числа виду 2016/679 чи 677/2001 — це посилання на
# закони та сам наказ, а не номери справ.
_NOISE_RE = re.compile(
    r"Regulament|Legii|Legea|Confiden|Str\.|Tel/FAX|e-mail|www\.|Pagina|Facebook"
    r"|ANEXA|ORDIN|Autoritatea",
    re.IGNORECASE,
)


def parse_index(html):
    """Дістає зі сторінки список наказів: рік, дата, номер, посилання на PDF."""
    soup = BeautifulSoup(html, "lxml")
    orders = []
    current_year = None

    for node in soup.find_all(["p", "li"]):
        text = node.get_text(" ", strip=True)

        if node.name == "p":
            candidate = text.strip()
            if _YEAR_RE.match(candidate):
                current_year = int(candidate)
            continue

        links = [a for a in node.find_all("a", href=True) if ".pdf" in a["href"].lower()]
        if not links:
            continue

        date_match = _DATE_RE.search(text)
        if not date_match:
            continue
        date = date_match.group(1)
        year = int(date.split(".")[-1]) if not current_year else current_year

        for link in links:
            number = link.get_text(" ", strip=True).strip() or None
            url = link["href"]
            if url.startswith("/"):
                url = BASE_URL + url
            orders.append(
                {
                    "number": number,
                    "date": date,
                    "year": year,
                    "pdf_url": url,
                }
            )

    return orders


def _extract_pdf_text(data):
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def parse_pdf(data):
    """Повертає (список_справ, текст). Кожна справа: номер, рік, к-ть дітей.

    `case_number` — як надруковано в PDF, `search_key` — уніфікований вигляд
    «номер/рік», щоб справу з сегментом RD теж можна було знайти за 48031/2023.
    """
    text = _extract_pdf_text(data)
    cases = []
    seen = set()

    for line in text.splitlines():
        if _NOISE_RE.search(line):
            continue
        minors_match = _MINORS_RE.search(line)
        minors = int(minors_match.group(1)) if minors_match else 0

        for number, middle, year in _CASE_RE.findall(line):
            search_key = "%s/%s" % (number, year)
            case_number = "%s/%s/%s" % (number, middle, year) if middle else search_key
            if case_number in seen:
                continue
            seen.add(case_number)
            cases.append(
                {
                    "case_number": case_number,
                    "search_key": search_key,
                    "case_year": int(year),
                    "minors": minors,
                    "raw": line.strip(),
                }
            )

    return cases, text
