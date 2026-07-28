.DEFAULT_GOAL := help
.PHONY: help build up down restart logs ps sh sync sync-fast stats search test \
        db-export db-import clean venv dev cli-sync cli-stats

COMPOSE ?= docker compose
SERVICE ?= web
PORT    ?= 8000
WORKERS ?= 4
PY      ?= .venv/bin/python

# Одноразовий контейнер із тим самим томом — не чіпає працюючий веб.
RUN = $(COMPOSE) run --rm $(SERVICE)

help: ## Показати цю довідку
	@awk 'BEGIN {FS = ":.*?## "} \
		/^##@ / {printf "\n\033[1m%s\033[0m\n", substr($$0, 5)} \
		/^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo ""
	@echo "Приклади:  make up  ·  make sync WORKERS=8  ·  make search Q=32805/2023"

##@ Docker

build: ## Зібрати образ
	$(COMPOSE) build

up: ## Підняти веб на http://localhost:8000
	$(COMPOSE) up -d
	@echo "→ http://localhost:$(PORT)"

down: ## Зупинити контейнери (дані в томі лишаються)
	$(COMPOSE) down

restart: down up ## Перезапустити

logs: ## Дивитись логи (Ctrl+C — вийти)
	$(COMPOSE) logs -f $(SERVICE)

ps: ## Статус контейнерів
	$(COMPOSE) ps

sh: ## Shell усередині контейнера
	$(RUN) bash

##@ Дані

sync: ## Повна синхронізація: список наказів + розбір усіх PDF
	$(RUN) python cli.py sync --workers $(WORKERS)

sync-fast: ## Пробний прогін на 20 наказах (перевірити, що все живе)
	$(RUN) python cli.py sync --workers $(WORKERS) --limit 20

sync-retry: ## Синхронізація, ігноруючи паузи після невдалих спроб
	$(RUN) python cli.py sync --workers $(WORKERS) --retry-failed

test: ## Запустити перевірки без звернення до сайту міністерства
	$(RUN) python -m unittest discover -s tests -v

stats: ## Скільки наказів і справ у базі
	$(RUN) python cli.py stats

search: ## Знайти справу: make search Q=32805/2023
	@test -n "$(Q)" || { echo "вкажіть номер: make search Q=32805/2023"; exit 1; }
	$(RUN) python cli.py search "$(Q)"

# Копіювати сам файл бази наосліп не можна: у режимі WAL частина свіжих даних
# лежить у -wal, а старий -wal поверх підміненої бази взагалі відкотить її до
# попереднього стану. Тому експорт іде через VACUUM INTO (цілісний знімок одним
# файлом), а імпорт спершу прибирає -wal/-shm.
db-export: ## Витягнути базу з тому у ./data.sqlite3
	$(COMPOSE) up -d $(SERVICE)
	$(RUN) rm -f /data/export.sqlite3
	$(RUN) python -c "import os, sqlite3; sqlite3.connect(os.environ['CETATENIE_DB']).execute('VACUUM INTO ?', ['/data/export.sqlite3'])"
	$(COMPOSE) cp $(SERVICE):/data/export.sqlite3 ./data.sqlite3
	$(RUN) rm -f /data/export.sqlite3
	@ls -lh data.sqlite3

db-import: ## Залити локальну ./data.sqlite3 у том (щоб не синкати з нуля)
	@test -f data.sqlite3 || { echo "немає ./data.sqlite3 — спершу make db-export"; exit 1; }
	$(COMPOSE) up -d $(SERVICE)
	$(COMPOSE) stop $(SERVICE)
	$(RUN) rm -f /data/data.sqlite3 /data/data.sqlite3-wal /data/data.sqlite3-shm
	$(COMPOSE) cp ./data.sqlite3 $(SERVICE):/data/data.sqlite3
	# cp переносить власника з хоста, а в контейнері працює uid 10001 — без
	# цього рядка застосунок відкриє базу тільки на читання.
	$(COMPOSE) run --rm --user root $(SERVICE) chown 10001:10001 /data/data.sqlite3
	$(COMPOSE) up -d $(SERVICE)

clean: ## Знести контейнери РАЗОМ із базою у томі
	$(COMPOSE) down -v

##@ Локально, без Docker

venv: ## Створити .venv і поставити залежності
	python3 -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt

dev: ## Запустити Flask локально на :8000
	PORT=$(PORT) $(PY) app.py

cli-sync: ## Синхронізація локально
	$(PY) cli.py sync --workers $(WORKERS)

cli-stats: ## Статистика локально
	$(PY) cli.py stats
