"""Веб-інтерфейс до парсера наказів cetatenie.just.ro (art. 11)."""

import logging
import os

from flask import Flask, jsonify, redirect, render_template, request, url_for

from cetatenie import db, sync

app = Flask(__name__)
db.init()


@app.route("/")
def index():
    year = request.args.get("year", type=int)
    date_from = request.args.get("from") or None
    date_to = request.args.get("to") or None

    totals, years = db.stats()
    orders = db.list_orders(year=year, date_from=date_from, date_to=date_to)
    return render_template(
        "index.html",
        orders=orders,
        totals=totals,
        years=years,
        year=year,
        date_from=date_from or "",
        date_to=date_to or "",
        sync_state=sync.state(),
    )


@app.route("/search")
def search():
    query = (request.args.get("q") or "").strip()
    results = db.search_case(query) if query else []
    totals, _ = db.stats()
    return render_template("search.html", query=query, results=results, totals=totals)


@app.route("/order/<int:order_id>")
def order(order_id):
    order_row, cases = db.get_order(order_id)
    if not order_row:
        return "Наказ не знайдено", 404
    return render_template("order.html", order=order_row, cases=cases)


@app.route("/sync", methods=["POST"])
def start_sync():
    sync.run_background(workers=int(request.form.get("workers", 4)))
    return redirect(url_for("index"))


@app.route("/sync/status")
def sync_status():
    return jsonify(sync.state())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 8000)), debug=False)
