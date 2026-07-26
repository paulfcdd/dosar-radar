"""CLI: синхронізація та пошук без веб-інтерфейсу."""

import argparse
import logging

from cetatenie import db, sync


def main():
    parser = argparse.ArgumentParser(description="Парсер наказів cetatenie.just.ro (art. 11)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="оновити список наказів і розібрати PDF")
    p_sync.add_argument("--workers", type=int, default=4)
    p_sync.add_argument("--limit", type=int, help="розібрати лише N наказів")
    p_sync.add_argument("--no-refresh", action="store_true", help="не перечитувати список")

    p_search = sub.add_parser("search", help="знайти справу за номером")
    p_search.add_argument("number", help="напр. 32805/2023")

    sub.add_parser("stats", help="показати статистику")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    db.init()

    if args.command == "sync":
        count = sync.run(workers=args.workers, limit=args.limit, refresh=not args.no_refresh)
        print("оброблено наказів: %d" % count)
    elif args.command == "search":
        results = db.search_case(args.number)
        if not results:
            print("справу %s не знайдено" % args.number)
            return
        for r in results:
            print("%s — наказ %s від %s (діти: %d)\n  %s"
                  % (r["case_number"], r["number"], r["date"], r["minors"], r["pdf_url"]))
    elif args.command == "stats":
        totals, years = db.stats()
        print("накази: %d (розібрано %d, помилок %d), справи: %d"
              % (totals["orders"], totals["parsed"], totals["errors"], totals["cases"]))
        for y in years:
            print("  %d: %d наказів, %d справ" % (y["year"], y["orders"], y["cases"]))


if __name__ == "__main__":
    main()
