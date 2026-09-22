# Bequeme Kurzbefehle. Alles laesst sich auch direkt mit `docker compose` tun.
COMPOSE ?= docker compose

.PHONY: help setup up down restart rebuild logs check run seed backfill status shell test reset env

help:
	@echo "make setup    - Erstmalige Einrichtung (Docker pruefen, .env, Start)"
	@echo "make up       - Container starten bzw. ersetzen (nach Aenderung an .env)"
	@echo "make down     - Container stoppen"
	@echo "make restart  - Container neu starten (nach Aenderung an config.yaml)"
	@echo "make rebuild  - Image neu bauen und starten (nach Code-Aenderung)"
	@echo "make env      - Zeigt, welche Werte im laufenden Container wirklich gesetzt sind"
	@echo "make logs     - Logs verfolgen"
	@echo "make check    - Konfiguration, Finnhub und yfinance pruefen"
	@echo "make run      - Pipeline-Lauf sofort ausfuehren"
	@echo "make seed     - Letzte Berichtssaison einmalig nachladen (DAYS=150)"
	@echo "make backfill - Kurshistorie nachladen (noetig fuer Momentum)"
	@echo "make status   - Bestand zusammenfassen"
	@echo "make test     - Testsuite im Container ausfuehren"
	@echo "make shell    - Shell im Container"
	@echo "make reset    - Datenbank loeschen (fragt nach)"

setup:
	./setup.sh

# 'up' statt 'restart' nach einer .env-Aenderung: 'docker compose restart' startet
# denselben Container neu, dessen Umgebungsvariablen bei seiner Erstellung gesetzt
# wurden. Erst 'up -d' erkennt die geaenderte Konfiguration und ersetzt ihn.
up:
	$(COMPOSE) up -d

env:
	@$(COMPOSE) exec app printenv FINNHUB_API_KEY LOG_LEVEL RUN_ON_STARTUP TZ DB_PATH

down:
	$(COMPOSE) down

# Nur fuer config.yaml - die Datei ist gemountet und wird beim Start neu gelesen.
# Fuer .env siehe 'make up'.
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

# Einmalig nach der Einrichtung: holt Earnings weit zurueck, damit sich die
# Kette pruefen laesst, ohne auf die naechste Berichtssaison zu warten.
DAYS ?= 150
seed:
	$(COMPOSE) exec -u app app python -m app.cli seed --days $(DAYS)

# Nach einer Erhoehung von price_backfill_days - der Tageslauf holt sonst nur
# das kurze Aktualisierungsfenster.
backfill:
	$(COMPOSE) exec -u app app python -m app.cli backfill

status:
	$(COMPOSE) exec -u app app python -m app.cli status

shell:
	$(COMPOSE) exec app bash

test:
	$(COMPOSE) run --rm --no-deps -e FINNHUB_API_KEY=test -v "$(PWD)/tests:/app/tests:ro" \
		-v "$(PWD)/pytest.ini:/app/pytest.ini:ro" -v "$(PWD)/tools:/app/tools:ro" \
		--entrypoint sh app -c "pip install --quiet --root-user-action=ignore pytest && python -m pytest"

reset:
	@read -p "Datenbank ./data/poc.db wirklich loeschen? [j/N] " a; \
	 if [ "$$a" = "j" ] || [ "$$a" = "J" ]; then \
	   $(COMPOSE) down; rm -f data/poc.db data/poc.db-wal data/poc.db-shm; \
	   echo "Geloescht. Start mit: make up"; \
	 else echo "Abgebrochen."; fi
