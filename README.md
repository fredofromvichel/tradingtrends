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

Jede Seite trägt eine aufklappbare **Legende**, die alle Begriffe für Einsteiger
erklärt — PEAD, EPS, Surprise, SUE, Handelstage, Konfidenzintervall. Zusätzlich
hat jede Spaltenüberschrift einen Tooltip (gepunktete Unterlinie), und das
Mouseover auf einem Symbol zeigt Firmenname, Branche und Börse.

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
make seed       # letzte Berichtssaison einmalig nachladen (DAYS=150)
make logs       # Logs verfolgen
make status     # Bestand zusammenfassen
make up         # nach Änderung an .env  (ersetzt den Container)
make restart    # nach Änderung an config.yaml
make rebuild    # nach Änderung am Code
make env        # zeigt die im Container wirklich gesetzten Werte
make test       # Testsuite im Container
make reset      # Datenbank löschen (fragt nach)
```

Alles ohne `make` genauso möglich, z. B.
`docker compose exec -u app app python -m app.cli check`.

### Nach der Einrichtung: Seed-Lauf

Im Normalbetrieb schaut die Pipeline nur `lookback_days_earnings` (Default: 3)
Tage zurück. Außerhalb der Berichtssaison findet sie dabei korrekterweise
**nichts** — die Startseite bleibt leer, obwohl alles funktioniert. US-Zahlen
kommen geballt Mitte Januar, April, Juli und Oktober.

Damit sich die Kette sofort prüfen lässt, statt auf die nächste Saison zu warten:

```bash
make seed              # 150 Tage zurück
make seed DAYS=400     # über ein Jahr, mehrere Quartale
```

Der Befehl zieht das Ereignisfenster einmalig auf, holt die passende
Kurshistorie dazu (Ereignisfenster + Haltedauer) und legt die Signale an. Da die
Haltedauer bei alten Ereignissen längst abgelaufen ist, werden die Positionen im
selben Lauf geschlossen — unter `/history` stehen danach Ein- und Ausstieg mit
Rendite. Der Lauf dauert ein bis zwei Minuten (zwei Finnhub-Requests je Ticker)
und ist beliebig wiederholbar, ohne Duplikate zu erzeugen.

> **Das ist kein Backtest.** Die Signale rechnen mit den Zahlen, wie sie *heute*
> bei Finnhub stehen. Konsensschätzungen und berichtete EPS werden nachträglich
> revidiert, und die Kurse sind auf den heutigen Stand bereinigt — was damals
> tatsächlich bekannt war, lässt sich daraus nicht rekonstruieren. Die Ergebnisse
> belegen, dass die Pipeline rechnet, nicht dass die Strategie trägt. Belastbar
> ist nur das Forward-Tracking ab jetzt.

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

### Änderungen übernehmen

| Geändert | Befehl | Warum |
|---|---|---|
| `config.yaml` | `make restart` | Die Datei ist gemountet und wird beim Start neu gelesen. |
| `.env` | `make up` | `docker compose restart` startet **denselben** Container neu — dessen Umgebungsvariablen wurden bei seiner Erstellung gesetzt und ändern sich dabei nicht. Erst `up -d` erkennt die geänderte Konfiguration und ersetzt den Container. |
| Code unter `app/` | `make rebuild` | Der Code liegt im Image, nicht im Mount. |

In allen drei Fällen bleiben die Daten erhalten — `./data/poc.db` liegt auf dem
Host. Womit der Container tatsächlich läuft, zeigt `make env`.

Beim Start validiert die Anwendung die Konfiguration und bricht mit einer
konkreten Meldung ab, wenn etwas unstimmig ist (fehlender Key, vertauschte
Schwellen, Prozentangabe statt Bruch, zu kurze Kurshistorie).

---

## Kennzahlen und ihre Unsicherheit

Die Startseite zeigt Trefferquote und mittlere Rendite **erst ab 20
geschlossenen Positionen** als Zahl. Darunter steht statt eines Prozentwerts
nur `n = 4` — und daneben das 95 %-Konfidenzintervall.

Das ist Absicht und der wichtigste Punkt an der Auswertung. Bei drei Treffern
aus vier Positionen ergibt der Dreisatz „75 % Trefferquote". Das tatsächliche
Intervall reicht von rund 30 % bis 95 %: die Daten sind mit „die Strategie
funktioniert hervorragend" ebenso vereinbar wie mit „sie funktioniert
schlechter als ein Münzwurf". Ein Punktwert würde an dieser Stelle eine
Genauigkeit behaupten, die nicht existiert — auf einem Dashboard, das
Handelssignale zeigt, ist das kein kosmetisches Problem.

| Kennzahl | Verfahren | Aussage |
|---|---|---|
| Trefferquote | Wilson-Intervall | Auch bei kleinem n und Anteilen nahe 0 oder 1 brauchbar, anders als die Normalapproximation. |
| Ø Rendite | t-Intervall | Schließt das Intervall die Null aus, ist der Effekt von „kein Effekt" unterscheidbar. Andernfalls nicht — unabhängig davon, wie gut der Mittelwert aussieht. |
| Median, Spanne | rein beschreibend | Keine Inferenz, nur was die Stichprobe enthält. |

Die Grenze von 20 ist eine Konvention, keine magische Zahl (`MIN_SAMPLE` in
`app/stats.py`). Auch bei 20 Positionen ist das Intervall noch breit — es ist
nur nicht mehr völlig nichtssagend. Für eine belastbare Aussage über die
PEAD-Strategie bräuchte es ein Vielfaches davon, über mehrere Berichtssaisons
und mit vorher festgelegten Parametern.

**Bewusst nicht gebaut:** eine Trendprognose oder Erfolgswahrscheinlichkeit je
Signal. Aus dieser Datenlage wäre das eine erfundene Zahl. Was die Seite
stattdessen zeigt, ist der faktische Verlauf jeder offenen Position — wie weit
die Haltedauer fortgeschritten ist und wo der Kurs gerade steht.

---

## Wie die Pipeline arbeitet

Ein Durchlauf (`pipeline.run_daily()`), identisch ob vom Scheduler, vom Button
oder von der CLI ausgelöst:

0. **Stammdaten** — einmalig je Ticker: Firmenname, Branche und Börse über
   Finnhub `/stock/profile2`, nur für Symbole, bei denen sie noch fehlen. Danach
   wird der Schritt übersprungen. Schlägt er fehl, fällt die Anzeige auf das
   Symbol zurück und der Rest des Laufs geht weiter.
1. **Earnings abrufen** — Finnhub `/calendar/earnings` für das Rückblickfenster,
   `/stock/earnings` für die Surprise-Historie je Titel.
2. **Events verarbeiten** — `surprise_pct = (actual − estimate) / |estimate|`.
   Liegen ≥ `min_history_for_sue` Vorquartale vor:
   `sue = surprise_pct / stdev(historische surprise_pct)`.
3. **Signal-Entscheidung** — SUE hat Vorrang; ohne SUE greift die
   `surprise_pct`-Schwelle. Schwellen sind exklusiv (genau auf der Grenze → kein Signal).
4. **Kursdaten** — yfinance, Tages-OHLCV, split- und dividendenbereinigt.
5. **Neue Signale anlegen** — Status `OPEN`, Einstieg am ersten **handelbaren**
   Schlusskurs nach der Meldung.
6. **Offene Positionen prüfen** — sind `holding_period_days` Handelstage seit dem
   Einstieg vergangen, wird geschlossen und `return_pct` berechnet. Fehlt der
   Einstiegskurs noch, wird er hier nachgetragen.

Schritt 5 läuft vor Schritt 6. Im Tagesbetrieb ändert die Reihenfolge nichts —
ein heute eröffnetes Signal kann heute nicht fällig sein. Beim Nachladen alter
Earnings (`make seed`) ist die Haltedauer dagegen längst vorbei, und die Position
wird so im selben Lauf geschlossen statt erst im nächsten.

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

Beim Start werden fehlende Tabellen **und fehlende Spalten** ergänzt. Ein
`git pull` mit neuen Feldern kostet daher nicht die bereits verfolgten Signale.
Geänderte Typen oder neue Constraints deckt das nicht ab — dafür bleibt
`make reset`.

```
app/
├── main.py              FastAPI-App, Routen, Lifespan
├── config.py            config.yaml + Env, mit Validierung
├── models.py            SQLAlchemy-Modelle
├── db.py                Engine, Session, Ticker-Abgleich
├── signals.py           Signal-Logik (netz- und DB-frei, voll getestet)
├── stats.py             Konfidenzintervalle, Sperre kleiner Stichproben
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
| `tickers` | `symbol` | Beobachtete Titel, `active` statt Löschen, Firmenname/Branche/Börse |
| `earnings_events` | `id`, unique `(symbol, report_date)` | EPS, `surprise_pct`, `sue`, `report_hour`, `processed` |
| `prices` | `(symbol, date)` | Tages-OHLCV, bereinigt |
| `signals` | `id`, unique `earnings_event_id` | Ein- und Ausstieg, Status, `return_pct` |
| `pipeline_runs` | `id` | Protokoll je Lauf, Basis der Statusanzeige |

---

## Tests

```bash
make test
```

58 Tests, ohne Netzzugriff:

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
| `make check` meldet 401 | Key ungültig oder nicht übernommen. In `.env` korrigieren und `make up` (nicht `restart`), dann mit `make env` gegenprüfen. |
| `make check` meldet `/calendar/earnings` nicht verfügbar | Endpunkt im Tarif gesperrt. Die Pipeline weicht auf die Fiskalperiode aus — das Meldedatum ist dann eine Näherung. |
| Lauf endet `PARTIAL` | Eine Quelle ist ausgefallen, die andere lief durch. Ursache im Footer und in `make logs`. |
| Grünes Banner „Lauf abgeschlossen", aber die Seite bleibt leer | Normal, kein Fehler. Es entstehen nur Signale, wenn im Rückblickfenster von 3 Tagen gemeldet **und** die Schwelle überschritten wurde. Außerhalb der Berichtssaison passiert beides nicht. `make status` zeigt `events_count`, `/events` die Rohdaten. Mit `make seed` die letzte Saison nachholen. |
| `make seed` findet trotzdem nichts | Fenster vergrößern (`make seed DAYS=400`); oder `/calendar/earnings` ist im Tarif gesperrt (`make check` zeigt es); oder die Ticker sind keine US-Titel. |
| Keine Kursdaten für ein Symbol | Schreibweise gegen Yahoo Finance prüfen (Xetra z. B. `SAP.DE`). |
| `./data/poc.db` gehört root | `APP_UID`/`APP_GID` in `.env` auf die eigene ID setzen, `make up`. |
| Port 8000 belegt | `HOST_PORT` in `.env` ändern, `make up`. |

Die SQLite-Datei liegt auf dem Host unter `./data/poc.db` und übersteht
Neustarts wie Rebuilds. `make reset` löscht sie — danach beginnt das
Forward-Tracking von vorn.
