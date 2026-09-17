# PEAD-Signal-POC

Docker-Testumgebung für eine Post-Earnings-Announcement-Drift-Pipeline:
ruft täglich Earnings-Überraschungen für eine Ticker-Liste ab, berechnet daraus
BUY/SELL-Signale, verfolgt sie über eine feste Haltedauer und zeigt offene wie
geschlossene Positionen in einem lokalen Web-Frontend.

> **Aussagekraft.** 20 Titel liefern realistisch 5–10 Earnings-Events pro Quartal.
> Das reicht, um die Pipeline technisch zu verifizieren — nicht, um eine statistisch
> belastbare Aussage über die Strategie zu treffen. Keine Handelsausführung, kein
> Backtesting, keine Produktionshärtung.

---

## Schnellstart auf dem Debian-Server

```bash
git clone <repo-url> tradingtrends
cd tradingtrends
./setup.sh
```

`setup.sh` prüft Docker, legt `.env` an, fragt nach dem Finnhub-Key, trägt die
eigene UID/GID ein (damit `./data/poc.db` nicht root gehört), baut das Image und
wartet, bis die Anwendung antwortet. Danach steht die Zugriffs-URL im Terminal.

Ist Docker noch nicht installiert:

```bash
./setup.sh --install-docker
```

Ohne Rückfrage, z. B. für ein Provisioning-Skript:

```bash
FINNHUB_API_KEY=xxxxx ./setup.sh
```

### Voraussetzungen

* Debian 11/12 (oder jede andere Distribution mit Docker)
* Docker Engine + Compose-Plugin — `setup.sh --install-docker` erledigt das
* Ein **Finnhub-API-Key**, Free-Tier genügt: https://finnhub.io/register
  Ohne Key startet der Container nicht und sagt im Log, was fehlt.
* Ausgehende HTTPS-Verbindungen zu `finnhub.io` und `query*.finance.yahoo.com`

### Ohne setup.sh

```bash
cp .env.example .env
$EDITOR .env                     # FINNHUB_API_KEY eintragen
echo "APP_UID=$(id -u)" >> .env  # sonst gehört ./data/poc.db dem root
echo "APP_GID=$(id -g)" >> .env
docker compose up -d --build
```

---

## Zugriff

Der Port ist bewusst **nur auf das Loopback-Interface** des Servers gebunden
(`127.0.0.1:8000`). Die Anwendung hat keine Authentifizierung — sie darf nicht
offen im Netz stehen. Zugriff vom Arbeitsrechner per SSH-Tunnel:

```bash
ssh -N -L 8000:localhost:8000 user@dein-server
# danach im Browser: http://localhost:8000
```

Soll die Oberfläche direkt im LAN erreichbar sein, muss vorher eine
Authentifizierung davor (Reverse-Proxy mit Basic-Auth o. ä.). Erst dann in der
`.env` `BIND_ADDR=0.0.0.0` setzen.

### Seiten

| Pfad | Inhalt |
|---|---|
| `/` | Offene Signale, Kennzahlen, Button „Jetzt aktualisieren" |
| `/history` | Geschlossene Signale mit realisierter Rendite |
| `/events` | Erfasste Earnings-Events (Rohdaten-Kontrolle) |
| `/docs` | Interaktive OpenAPI-Dokumentation |

### JSON-API

| Methode | Pfad | Beschreibung |
|---|---|---|
| `GET` | `/api/signals?status=open\|closed` | Signale, ohne Parameter alle |
| `GET` | `/api/earnings-events?limit=200` | Erfasste Earnings-Events |
| `GET` | `/api/summary` | Kennzahlen |
| `GET` | `/healthz` | Healthcheck |
| `POST` | `/run-now` | Pipeline-Lauf, danach Redirect auf `/` |

---

## Bedienung

```bash
make help       # alle Kurzbefehle
make check      # Konfiguration, Finnhub und yfinance prüfen  <- erster Schritt bei Problemen
make run        # Pipeline-Lauf sofort ausführen
make logs       # Logs verfolgen
make status     # Bestand zusammenfassen
make restart    # nach Änderung an config.yaml
make rebuild    # nach Änderung am Code
make test       # Testsuite im Container
make reset      # Datenbank löschen (fragt nach)
```

Alles ohne `make` genauso möglich, z. B.
`docker compose exec -u app app python -m app.cli check`.

---

## Konfiguration

**`config.yaml`** — Ticker und Parameter. Die Datei ist ins Image gemountet,
Änderungen brauchen nur `make restart`, keinen Rebuild.

> Die ausgelieferte Ticker-Liste ist ein **Platzhalter**. Vor dem ersten
> ernsthaften Lauf dort die eigenen 20 Symbole eintragen.

| Parameter | Default | Bedeutung |
|---|---|---|
| `tickers` | 20 US-Large-Caps (Platzhalter) | Beobachtete Symbole |
| `sue_threshold_buy` / `_sell` | `1.0` / `-1.0` | SUE-Schwellen, exklusiv |
| `surprise_pct_fallback_buy` / `_sell` | `0.05` / `-0.05` | Fallback ohne Historie, als Bruch (0.05 = 5 %) |
| `min_history_for_sue` | `4` | Ab wie vielen Vorquartalen SUE berechnet wird |
| `holding_period_days` | `20` | Haltedauer in **Handelstagen** |
| `lookback_days_earnings` | `3` | Abruffenster des Earnings-Kalenders |
| `price_backfill_days` | `180` | Kurshistorie beim ersten Lauf |
| `price_refresh_days` | `7` | Kursfenster bei Folgeläufen |
| `schedule.cron` | `30 22 * * 1-5` | Werktags 22:30, nach US-Börsenschluss |
| `schedule.timezone` | `Europe/Berlin` | Zeitzone des Cron-Ausdrucks |
| `schedule.enabled` | `true` | Scheduler abschaltbar |

**`.env`** — Secrets und Host-Einstellungen: `FINNHUB_API_KEY`, `BIND_ADDR`,
`HOST_PORT`, `LOG_LEVEL`, `RUN_ON_STARTUP`, `APP_UID`, `APP_GID`.

Beim Start validiert die Anwendung die Konfiguration und bricht mit einer
konkreten Meldung ab, wenn etwas unstimmig ist (fehlender Key, vertauschte
Schwellen, Prozentangabe statt Bruch, zu kurze Kurshistorie).

---

## Wie die Pipeline arbeitet

Ein Durchlauf (`pipeline.run_daily()`), identisch ob vom Scheduler, vom Button
oder von der CLI ausgelöst:

1. **Earnings abrufen** — Finnhub `/calendar/earnings` für das Rückblickfenster,
   `/stock/earnings` für die Surprise-Historie je Titel.
2. **Events verarbeiten** — `surprise_pct = (actual − estimate) / |estimate|`.
   Liegen ≥ `min_history_for_sue` Vorquartale vor:
   `sue = surprise_pct / stdev(historische surprise_pct)`.
3. **Signal-Entscheidung** — SUE hat Vorrang; ohne SUE greift die
   `surprise_pct`-Schwelle. Schwellen sind exklusiv (genau auf der Grenze → kein Signal).
4. **Kursdaten** — yfinance, Tages-OHLCV, split- und dividendenbereinigt.
5. **Offene Positionen prüfen** — sind `holding_period_days` Handelstage seit dem
   Einstieg vergangen, wird geschlossen und `return_pct` berechnet.
6. **Neue Signale anlegen** — Status `OPEN`, Einstieg am ersten **handelbaren**
   Schlusskurs nach der Meldung. Fehlt der Kurs noch, trägt ihn der nächste Lauf nach.

Die vier Schritte sind gegeneinander abgeschottet: fällt Finnhub aus, laufen
Kursaktualisierung und Positionspflege trotzdem durch. Der Lauf wird dann als
`PARTIAL` protokolliert und die Ursache steht im Log und im Frontend-Footer.
Läufe sind idempotent — derselbe Lauf mehrfach ausgeführt erzeugt keine
Duplikate.

### Rechenkonventionen

* `surprise_pct` und `return_pct` sind **Brüche**: `0.05` = 5 %.
* Haltedauer zählt **Handelstage**, ermittelt aus den tatsächlich vorhandenen
  Kurszeilen — keine Annahmen über Feiertage.
* `SELL` wird als **Short** gerechnet: fallender Kurs ⇒ positive Rendite.
* **Einstiegstag.** Nur bei einer Meldung vor Handelsbeginn (Finnhub `hour = bmo`)
  ist der Schlusskurs des Meldetages selbst erreichbar. Bei `amc`, `dmh` oder
  unbekanntem Zeitpunkt wird der Schlusskurs des **Folgetages** genommen — sonst
  würde der Einstieg den Kurssprung über Nacht vorwegnehmen (Lookahead-Bias) und
  die gemessene Drift systematisch zu gut aussehen lassen. `/events` zeigt den
  erkannten Zeitpunkt je Event.
* Renditen sind **brutto** — ohne Gebühren, Slippage und Steuern.
* SUE standardisiert hier die *prozentuale* Überraschung. Die akademische
  Definition standardisiert die EPS-Differenz. Absichtlich so gewählt, weil der
  Konzeptentwurf es so vorgibt — bei einer Interpretation der Ergebnisse
  mitdenken.

---

## Architektur

```
Finnhub API ──┐
              ├──> Docker-Container ──> ./data/poc.db  (Host, Volume-Mount)
yfinance ─────┘      FastAPI (uvicorn)
                     ├─ HTML-Views (Jinja2)
                     ├─ JSON-API
                     └─ APScheduler (In-Process-Cron)
```

Ein einziger Anwendungscontainer, kein separater DB-Container. SQLite läuft im
WAL-Modus, damit das Frontend lesen kann, während ein Lauf schreibt.

```
app/
├── main.py              FastAPI-App, Routen, Lifespan
├── config.py            config.yaml + Env, mit Validierung
├── models.py            SQLAlchemy-Modelle
├── db.py                Engine, Session, Ticker-Abgleich
├── signals.py           Signal-Logik (netz- und DB-frei, voll getestet)
├── pipeline.py          Orchestrierung des Tageslaufs
├── scheduler.py         APScheduler
├── views.py             Aufbereitung für Frontend und API
├── cli.py               run / check / status
└── sources/
    ├── finnhub_client.py
    └── prices.py        yfinance
```

### Datenmodell

| Tabelle | Schlüssel | Inhalt |
|---|---|---|
| `tickers` | `symbol` | Beobachtete Titel, `active` statt Löschen |
| `earnings_events` | `id`, unique `(symbol, report_date)` | EPS, `surprise_pct`, `sue`, `report_hour`, `processed` |
| `prices` | `(symbol, date)` | Tages-OHLCV, bereinigt |
| `signals` | `id`, unique `earnings_event_id` | Ein- und Ausstieg, Status, `return_pct` |
| `pipeline_runs` | `id` | Protokoll je Lauf, Basis der Statusanzeige |

---

## Tests

```bash
make test
```

33 Tests, ohne Netzzugriff:

* `tests/test_signals.py` — Surprise, SUE, Schwellenwerte, Short-Rendite,
  Handelstags-Arithmetik über Wochenenden, Einstiegstag je Meldezeitpunkt.
* `tests/test_pipeline.py` — kompletter Durchlauf gegen Attrappen von Finnhub und
  yfinance: Event erfassen, Signal eröffnen, Position schließen, Idempotenz und
  Verhalten beim Ausfall einer Datenquelle.

---

## Fehlersuche

| Symptom | Ursache und Abhilfe |
|---|---|
| Container startet nicht, Log zeigt „START ABGEBROCHEN" | `FINNHUB_API_KEY` fehlt in `.env`. |
| `make check` meldet 401 | Key ungültig. In `.env` korrigieren, `make restart`. |
| `make check` meldet `/calendar/earnings` nicht verfügbar | Endpunkt im Tarif gesperrt. Die Pipeline weicht auf die Fiskalperiode aus — das Meldedatum ist dann eine Näherung. |
| Lauf endet `PARTIAL` | Eine Quelle ist ausgefallen, die andere lief durch. Ursache im Footer und in `make logs`. |
| Keine Signale nach dem ersten Lauf | Normal. Es entstehen nur Signale, wenn im Rückblickfenster gemeldet **und** die Schwelle überschritten wurde. `/events` zeigt, ob Daten ankommen. |
| Keine Kursdaten für ein Symbol | Schreibweise gegen Yahoo Finance prüfen (Xetra z. B. `SAP.DE`). |
| `./data/poc.db` gehört root | `APP_UID`/`APP_GID` in `.env` auf die eigene ID setzen, `make rebuild`. |
| Port 8000 belegt | `HOST_PORT` in `.env` ändern, `make up`. |

Die SQLite-Datei liegt auf dem Host unter `./data/poc.db` und übersteht
Neustarts wie Rebuilds. `make reset` löscht sie — danach beginnt das
Forward-Tracking von vorn.
