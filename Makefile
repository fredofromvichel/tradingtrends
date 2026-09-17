# Bequeme Kurzbefehle. Alles laesst sich auch direkt mit `docker compose` tun.
COMPOSE ?= docker compose

.PHONY: help setup up down restart rebuild logs check run status shell test reset

help:
	@echo "make setup    - Erstmalige Einrichtung (Docker pruefen, .env, Start)"
	@echo "make up       - Container starten"
	@echo "make down     - Container stoppen"
	@echo "make restart  - Container neu starten (nach Aenderung an config.yaml)"
	@echo "make rebuild  - Image neu bauen und starten (nach Code-Aenderung)"
	@echo "make logs     - Logs verfolgen"
	@echo "make check    - Konfiguration, Finnhub und yfinance pruefen"
	@echo "make run      - Pipeline-Lauf sofort ausfuehren"
	@echo "make status   - Bestand zusammenfassen"
	@echo "make test     - Testsuite im Container ausfuehren"
	@echo "make shell    - Shell im Container"
	@echo "make reset    - Datenbank loeschen (fragt nach)"

setup:
	./setup.sh

up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

restart:
	$(COMPOSE) restart

rebuild:
	$(COMPOSE) up -d --build

logs:
	$(COMPOSE) logs -f

check:
	$(COMPOSE) exec -u app app python -m app.cli check

run:
	$(COMPOSE) exec -u app app python -m app.cli run

status:
	$(COMPOSE) exec -u app app python -m app.cli status

shell:
	$(COMPOSE) exec app bash

test:
	$(COMPOSE) run --rm --no-deps -e FINNHUB_API_KEY=test -v "$(PWD)/tests:/app/tests:ro" \
		-v "$(PWD)/pytest.ini:/app/pytest.ini:ro" \
		--entrypoint sh app -c "pip install --quiet --root-user-action=ignore pytest && python -m pytest"

reset:
	@read -p "Datenbank ./data/poc.db wirklich loeschen? [j/N] " a; \
	 if [ "$$a" = "j" ] || [ "$$a" = "J" ]; then \
	   $(COMPOSE) down; rm -f data/poc.db data/poc.db-wal data/poc.db-shm; \
	   echo "Geloescht. Start mit: make up"; \
	 else echo "Abgebrochen."; fi
