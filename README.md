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

Zwei Betriebsarten, beide über die `.env`.

**Standard: nur lokal, Zugriff per SSH-Tunnel.** `BIND_ADDR=127.0.0.1` bindet
den Port ausschließlich auf das Loopback-Interface.

```bash
ssh -N -L 8000:localhost:8000 user@dein-server
# danach im Browser: http://localhost:8000
```

**Im Netz erreichbar, beschränkt auf bekannte Adressen.** `BIND_ADDR=0.0.0.0`
veröffentlicht den Port auf allen Schnittstellen; eine Middleware lässt dann
nur Absender aus `ALLOWED_IPS` durch:

```bash
BIND_ADDR=0.0.0.0
ALLOWED_IPS=203.0.113.7               # oder: 203.0.113.7,198.51.100.0/24
```

Danach `make up` (nicht `make restart` — siehe oben). Die eigene Adresse zeigt
`curl -s https://ifconfig.me`.

Drei Punkte, die dabei zählen:

- **Ohne `ALLOWED_IPS` startet der Container nicht**, sobald `BIND_ADDR` nicht
  mehr Loopback ist. Ein offener Dienst ohne Authentifizierung soll nicht aus
  Versehen entstehen.
- **Die Prüfung sitzt in der Anwendung, nicht in der Firewall.** Docker
  veröffentlicht Ports über eigene iptables-Ketten, an denen die üblichen
  `ufw`-Regeln der INPUT-Kette vorbeilaufen — eine Regel, die man dort
  einträgt, greift für veröffentlichte Container-Ports oft schlicht nicht. Wer
  zusätzlich eine Firewallregel will, muss sie in die Kette `DOCKER-USER`
  hängen.
- **`X-Forwarded-For` wird ignoriert.** Entschieden wird allein anhand der
  TCP-Gegenstelle. Ohne vorgeschalteten Reverse-Proxy kann diesen Kopf jeder
  Absender selbst setzen; wer ihm traut, hebt die Sperre auf. Localhost ist
  immer erlaubt, damit der Healthcheck im Container funktioniert.

Abgewiesene Anfragen bekommen 403 und sehen ihre eigene Adresse — praktisch,
wenn sich die IP geändert hat.

**Schutz der Formulare.** Die Oberfläche kann Titel aufnehmen und entfernen.
Dagegen hilft die IP-Sperre nicht allein: Eine fremde Webseite, die du im
selben Browser öffnest, könnte ein Formular an die Anwendung schicken — und die
Anfrage käme von deiner erlaubten Adresse. Deshalb prüft jede schreibende
Anfrage zweierlei (`app/csrf.py`): dass der `Origin`-Kopf des Browsers zur
Anwendung passt, und dass das Formular ein Token trägt, das eine fremde Seite
nicht lesen kann. Das Token gilt bis zum nächsten Neustart; ein vorher
geöffnetes Formular meldet sich danach als abgelaufen — neu laden genügt.

### Seiten

| Pfad | Inhalt |
|---|---|
| `/` | **Übersicht**: was demnächst ausläuft, nächste Termine, Kennzahlen beider Quellen, offene Positionen |
| `/history` | Geschlossene Signale mit realisierter Rendite |
| `/titel` | Tagesstatus aller Titel; Titel prüfen, aufnehmen, wieder aufnehmen, Liste sichern |
| `/titel/<SYMBOL>` | Steckbrief: PEAD-Status, Kursverlauf mit Signalphasen beider Strategien, Signale im Rückblick, Lesehilfe mit Beleglage, Termin, Analysten, Recherche-Links; Beobachtung beenden |
| `/momentum` | Zweite Signalquelle: Rangfolge, aktueller Korb, Kennzahlen |
| `/events` | Erfasste Earnings-Events (Rohdaten-Kontrolle) |
| `/docs` | Interaktive OpenAPI-Dokumentation |

Das Navigationskonzept folgt der Idee, dass der häufige Fall ohne Klicken
auskommt: die Übersicht beantwortet „steht etwas an, was läuft, was kommt"
auf einer Seite. Dazu kommen drei Hilfen, die den Weg zurück zur Liste
sparen:

- **Blättern auf dem Steckbrief.** Vor/Zurück zwischen Titeln, auch per
  Tastatur (`j`/`k` oder Pfeiltasten), mit Positionsanzeige „7 von 23".
- **Sortierbare Tabellen.** Klick oder Enter auf eine Spaltenüberschrift.
  Sortiert wird nach dem Rohwert, nicht nach dem angezeigten Text — sonst
  landete „+8,66 %" hinter „1 234,50".
- **Filter auf der Titelliste.** Freitext über Symbol, Firma und Branche,
  mit Trefferzähler.

Jede Seite trägt eine aufklappbare **Legende**, die alle Begriffe für Einsteiger
erklärt — PEAD, EPS, Surprise, SUE, Handelstage, Konfidenzintervall. Zusätzlich
hat jede Spaltenüberschrift einen Tooltip (gepunktete Unterlinie), und das
Mouseover auf einem Symbol zeigt Firmenname, Branche und Börse.

## Warum dieses Signal? Der Rechenweg

Eine Empfehlung, die man nicht nachrechnen kann, ist eine Behauptung. Deshalb
trägt jedes Signal einen **Rechenweg** — in Tabellen hinter dem Knopf
„Warum?", auf dem Steckbrief aufgeklappt über dem Chart. Er führt in sieben
Schritten vom Geschäftsbericht bis zur Rendite und nennt in jedem Schritt die
Rechnung mit den tatsächlichen Zahlen dieses Signals:

| Schritt | Was dort steht |
|---|---|
| Die Meldung | Gemeldeter Gewinn je Aktie gegen die Analystenschätzung |
| Die Überraschung | Abweichung in Prozent, mit der Division, aus der sie entsteht |
| Ist das viel? | Die Abweichung gemessen an der **eigenen** üblichen Schwankung des Unternehmens — das ist der SUE |
| Warum BUY/SELL | Welche Schwelle überschritten wurde und warum die Forschung daraus ein Signal ableitet |
| Einstieg | Welcher Schlusskurs genommen wurde und warum erst der Folgetag |
| Ausstieg | Kurs nach Ablauf der Haltedauer, mit der Renditerechnung |
| Einordnung | Was die **übrigen** beobachteten Titel im selben Zeitraum machten |

Der letzte Schritt ist der wichtigste und in `app/pipeline.py`
(`_benchmark_return`) hinterlegt: beim Schließen einer Position wird die
gleichgewichtete Rendite aller anderen aktiven Titel über **dasselbe**
Zeitfenster berechnet und als Spalte „Vergleich" mitgeführt. Eine Rendite von
+8,6 % sagt nichts, solange offen bleibt, ob der Gesamtmarkt in derselben Zeit
12 % gestiegen ist. Erst die Differenz ist die eigentliche Frage. Nötig sind
dafür mindestens drei Vergleichswerte; darunter bleibt die Spalte leer, statt
eine Scheinaussage zu erzeugen.

Momentum-Signale tragen denselben Rechenweg mit eigener Kette: Rangfolge,
warum der letzte Monat ausgespart bleibt, Platzierung im Universum,
Einstieg, Ausstieg, Einordnung. Dort steht zusätzlich ein Schritt „wichtig zu
verstehen", der auf die Relativität hinweist — ein LONG heißt „besser als die
anderen", nicht „gute Aktie".

Der Rechenweg wird aus den gespeicherten Werten erzeugt (`app/explain.py`),
nicht aus Textbausteinen: dafür halten `earnings_events.surprise_stdev` und
`.history_count` die Zutaten des SUE fest. Steht eine Zutat nicht in der
Datenbank, fehlt der Schritt — er wird nicht geraten.

### JSON-API

| Methode | Pfad | Beschreibung |
|---|---|---|
| `GET` | `/api/signals?status=open\|closed` | Signale, ohne Parameter alle |
| `GET` | `/api/earnings-events?limit=200` | Erfasste Earnings-Events |
| `GET` | `/api/tickers` | Tagesstatus aller Titel |
| `GET` | `/api/momentum?status=open\|closed` | Momentum-Positionen und Kennzahlen |
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
make backfill   # Kurshistorie sofort nachladen (sonst beim nächsten Lauf)
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

**`config.yaml`** — Parameter. Die Datei ist ins Image gemountet, Änderungen
brauchen nur `make restart`, keinen Rebuild.

### Titel verwalten

Die beobachteten Titel pflegst du in der Oberfläche unter **`/titel`**, nicht in
der `config.yaml`. Gespeichert wird in der Datenbank — ein `git pull` kann die
eigene Liste damit nicht mehr überschreiben.

**Aufnehmen.** Kürzel, Yahoo-Symbol oder Firmenname eingeben (mehrere mit
Komma), „Prüfen" klicken. Die Prüfung löst die Eingabe über die Yahoo-Suche in
Unternehmen und deren Handelsplätze auf und zeigt je Listing den letzten Kurs.
Das ist nötig, weil dieselbe Buchstabenfolge an verschiedenen Börsen oft eine
andere Firma ist:

| Eingabe | Was dahintersteckt |
|---|---|
| `AOMD` | NYSE: Angel Oak Mortgage REIT · Xetra/Frankfurt: **Alstom** — und nicht AMD |
| `AIR` | NYSE: AAR Corp · Xetra: **Airbus** |
| `SAP` | NYSE/Xetra: SAP SE · Toronto: Saputo · Johannesburg: Sappi |
| `NVD` | Xetra: NVIDIA — das US-Listing `NVDA` findet die Prüfung über den Namen |

Angeboten werden nur Aktien mit aktuellen Kursen — ETFs melden keine Earnings,
und ein Listing ohne Kurse könnte die Pipeline nicht verfolgen.

Passt eine Eingabe zu mehreren Unternehmen, steht ein Hinweis darüber und
**nichts ist vorausgewählt**, damit niemand versehentlich die falsche Firma
übernimmt. Je Unternehmen wird ein Handelsplatz bevorzugt:

1. **US-Börse** — nur dort liefert Finnhub im freien Tarif Earnings
   (siehe [Wenn Finnhub einen Titel nicht abdeckt](#wenn-finnhub-einen-titel-nicht-abdeckt)).
2. **Xetra** (`.DE`) — liquidester deutscher Handelsplatz, Kurse in Euro.
3. **Frankfurt** (`.F`), dann die Heimatbörse, dann Regionalbörsen, OTC zuletzt.

Nach dem Aufnehmen startet ein Lauf im Hintergrund und holt Kurshistorie,
Termine und Stammdaten; die Liste lädt sich währenddessen selbst neu. Läuft
gerade ein anderer Lauf, wartet er auf ihn. Mehrere Änderungen kurz
hintereinander teilen sich einen Lauf.

**Entfernen.** Auf dem Steckbrief unter dem Kurs: „Nicht mehr beobachten".
Gelöscht wird nichts — Kurshistorie und abgeschlossene Signale bleiben lesbar,
und **offene Positionen laufen bis zum regulären Ausstieg weiter**. Der Titel
bekommt dafür weiter Kurse, nur neue Signale entstehen nicht mehr. Die
Rückmeldung danach bietet „Rückgängig"; entfernte Titel stehen unten auf
`/titel` unter „Nicht mehr beobachtet" und lassen sich dort wieder aufnehmen.

**Sichern.** `/titel` → „Liste sichern" zeigt die aktuelle Liste als
`tickers:`-Block. Der `tickers`-Eintrag in der `config.yaml` ist nur noch die
**Startliste für eine leere Datenbank** — nach einer Neuinstallation oder
`make reset`. Steht dort etwas, das nicht beobachtet wird, sagt das Log beim
Start, dass die Datei an dieser Stelle nicht mehr wirkt.

> **Beim ersten Update auf diese Version:** Die Liste, die gerade läuft, steht
> bereits in der Datenbank und bleibt. Eine lokal geänderte `config.yaml`
> blockiert aber den `git pull`. Dann: `cp config.yaml ~/config.alt.yaml &&
> git checkout -- config.yaml && git pull && make rebuild`.

| Parameter | Default | Bedeutung |
|---|---|---|
| `tickers` | 20 US-Large-Caps | Startliste für eine leere Datenbank; gepflegt wird unter `/titel` |
| `sue_threshold_buy` / `_sell` | `1.0` / `-1.0` | SUE-Schwellen, exklusiv |
| `surprise_pct_fallback_buy` / `_sell` | `0.05` / `-0.05` | Fallback ohne Historie, als Bruch (0.05 = 5 %) |
| `min_history_for_sue` | `4` | Ab wie vielen Vorquartalen SUE berechnet wird |
| `holding_period_days` | `20` | Haltedauer in **Handelstagen** |
| `lookback_days_earnings` | `3` | Abruffenster des Earnings-Kalenders |
| `price_backfill_days` | `800` | Kurshistorie je Titel; wird einmal nachgeholt, wenn der Wert steigt |
| `price_refresh_days` | `7` | Kursfenster bei Folgeläufen |
| `schedule.cron` | `30 22 * * 1-5` | Werktags 22:30, nach US-Börsenschluss |
| `schedule.timezone` | `Europe/Berlin` | Zeitzone des Cron-Ausdrucks |
| `schedule.enabled` | `true` | Scheduler abschaltbar |

**`.env`** — Secrets und Host-Einstellungen: `FINNHUB_API_KEY`, `BIND_ADDR`,
`HOST_PORT`, `LOG_LEVEL`, `RUN_ON_STARTUP`, `APP_UID`, `APP_GID`.

### Änderungen übernehmen

| Geändert | Befehl | Warum |
|---|---|---|
| Titelliste | – | Wirkt sofort; die Oberfläche startet selbst einen Lauf. |
| `config.yaml` | `make restart` | Die Datei ist gemountet und wird beim Start neu gelesen. |
| `.env` | `make up` | `docker compose restart` startet **denselben** Container neu — dessen Umgebungsvariablen wurden bei seiner Erstellung gesetzt und ändern sich dabei nicht. Erst `up -d` erkennt die geänderte Konfiguration und ersetzt den Container. |
| Code unter `app/` | `make rebuild` | Der Code liegt im Image, nicht im Mount. |

In allen drei Fällen bleiben die Daten erhalten — `./data/poc.db` liegt auf dem
Host. Womit der Container tatsächlich läuft, zeigt `make env`.

Beim Start validiert die Anwendung die Konfiguration und bricht mit einer
konkreten Meldung ab, wenn etwas unstimmig ist (fehlender Key, vertauschte
Schwellen, Prozentangabe statt Bruch, zu kurze Kurshistorie).

---

## Der Titel-Steckbrief

`/titel/<SYMBOL>` beantwortet die Frage „wie steht dieser Titel gerade da?" —
und zwar getrennt nach dem, was diese Anwendung beurteilt, und dem, was sie nur
beschreibt oder von Dritten übernimmt.

**PEAD-Status.** Das Einzige, worüber das System ein Urteil fällt. Er kennt vier
Zustände: ein Signal läuft (mit Fortschritt der Haltedauer), die letzte Meldung
lag innerhalb der Schwellen, das Drift-Fenster ist abgelaufen, oder es gibt noch
keine Meldung. **„Keine Aussage" ist der Normalfall**, kein Fehler: die
PEAD-Logik äußert sich nur in den Handelstagen nach einer Gewinnüberraschung.
An den übrigen rund 340 Tagen im Jahr hat sie zu einem Titel nichts zu sagen,
und die Seite sagt genau das.

**Kursverlauf mit Signalphasen.** Ein Jahr Schlusskurse, darunter zwei Spuren
mit derselben Zeitachse: wann **PEAD** und wann **Momentum** long (blau) oder
short (orange) war — jede Haltephase als Balken vom Einstieg bis zum Ausstieg.
Voll gefüllt heißt *live erzeugt*, hell mit Kontur heißt *rückwirkend
berechnet* (siehe [Live und rückwirkend](#live-und-rückwirkend)). Die Spuren
liegen bewusst unter dem Kurs, nicht auf ihm: zwei Strategien, die sich zeitlich
überlappen, würden sich dort gegenseitig zudecken.

Über dem Diagramm schalten Häkchen die Ebenen: PEAD- und Momentum-Phasen,
Meldungen, **200-Tage-Linie**, **50-Tage-Linie**, **52-Wochen-Spanne**. Die Wahl
merkt sich der Browser. Das Fadenkreuz zeigt für jeden Tag den Kurs, die
Durchschnitte und jede Position, die an diesem Tag lief — mit Ergebnis,
Vergleich und Grund („SUE +1,81", „Rang 3 von 22") — und hebt deren
Haltephase auf der Kursfläche hervor. Auf dem Handy lässt sich das Diagramm
innerhalb der Karte seitlich wischen, statt bis zur Unlesbarkeit zu schrumpfen.

Die Farben sind mit dem Palette-Validator geprüft, alle Paare, beide
Farbmodi: Die 200-Tage-Linie ist aqua, die 50-Tage-Linie grau. Ocker oder
Orange schieden aus — für Rot-Schwache nicht vom Short-Orange zu trennen, und
damit leicht als Signal misszuverstehen.

**Signale im Rückblick.** Unter dem Diagramm jede Phase als Tabellenzeile:
Strategie, Richtung, Herkunft, Ein- und Ausstieg, Ergebnis, Vergleich, Grund —
mit einer Bilanz getrennt nach live und rückwirkend. Die Spalte *Vergleich* ist
die wichtigste: Eine Phase ist nur etwas wert, wenn sie besser lief als die
übrigen Titel im selben Zeitraum.

**Den Kurs selbst lesen.** Sieben Fragen an jeden Kursverlauf, jeweils mit dem
heutigen Wert, einem Satz zur Lesart für diesen Titel und der **Beleglage** —
wie gut die Forschung stützt, dass die Kennzahl etwas über die Zukunft sagt.
Sortiert nach Beleglage, Belastbares zuerst:

| Frage | Beleglage | Quellen |
|---|---|---|
| Lief der Titel besser als deine übrigen? (Momentum-Rang) | gut belegt | Jegadeesh & Titman 1993; Asness, Moskowitz & Pedersen 2013; Daniel & Moskowitz 2016 |
| Wie weit vom 52-Wochen-Hoch? | belegt | George & Hwang 2004 |
| Wie viel Risiko? (Schwankung, größter Rückgang) | gut belegt als Risikomaß | Ang, Hodrick, Xing & Zhang 2006; Baker, Bradley & Wurgler 2011 |
| War der letzte Monat außergewöhnlich? (Kurzfrist-Umkehr) | belegt, bei großen Titeln schwach | Jegadeesh 1990; Lehmann 1990; Avramov, Chordia & Goyal 2006 |
| Zeigt der langfristige Trend nach oben? (200-Tage-Linie) | gemischt | Brock, Lakonishok & LeBaron 1992; Sullivan, Timmermann & White 1999 |
| Liegt der kurze Schnitt über dem langen? (50/200-Kreuzung) | schwach | Sullivan, Timmermann & White 1999 |
| Wird ungewöhnlich viel gehandelt? | beschreibend | – |

Der Lerngehalt steckt in der Reihenfolge: Die bekanntesten Chartsignale — die
Kreuzung zweier Durchschnitte — sind die am schwächsten belegten. „Im Chart
zeigen" schaltet die passende Ebene ein. Darunter eine Schrittfolge, wie man
einen Titel durchgeht, und die Kursveränderung über eine Woche bis zwölf
Monate. Die Lesarten beschreiben, was ein Wert bedeutet — nicht, was zu tun ist.

**Nächste Quartalszahlen.** Der Termin aus dem Finnhub-Kalender, also wann
überhaupt wieder mit einem Signal zu rechnen ist.

**Analystenbild.** Verteilung Kaufen/Halten/Verkaufen von Finnhub. Fremddaten,
als solche gekennzeichnet, kein Bestandteil der PEAD-Logik.

### Recherche-Links

Statt Schlagzeilen in die Anwendung zu holen (die dort sofort veralten), führen
vorgefertigte Suchen nach außen — jede auf kursrelevante Treffer eingegrenzt:

| Link | Was gesucht wird |
|---|---|
| Nachrichten der letzten Tage | Firmierung, Kurzname und Kürzel des Unternehmens |
| Kursrelevante Ereignisse | dazu Quartalszahlen, Gewinnwarnung, Übernahme, Klage, Rückruf … |
| Analysten und Kursziele | dazu Kursziel, hoch-/abgestuft, upgrade, downgrade |
| Branche | die übersetzte Branchenbezeichnung (`Semiconductors` → Halbleiterbranche, Chipindustrie) |
| Branchenereignisse | dazu Regulierung, Zölle, Exportkontrolle, Lieferkette, Subventionen |
| SEC-Pflichtmitteilungen | 8-K-Meldungen im Original, ohne journalistische Zwischenstufe |

Ein Beispiel, wie es beim Klick tatsächlich abgeschickt wird:

```
("NVIDIA Corp" OR NVIDIA OR NVDA) (Quartalszahlen OR Gewinnwarnung OR Prognose
 OR Übernahme OR Rückruf OR Klage OR ... OR "profit warning") when:30d
```

Zwei Details, die die Trefferqualität ausmachen: die Rechtsform wird für die
Suche abgetrennt (`Apple Inc` → auch `Apple`, weil Schlagzeilen selten die
vollständige Firmierung nennen), und Kürzel mit weniger als drei Zeichen fliegen
raus — `V` für Visa oder `F` für Ford würde alles treffen.

Alle Begriffe stehen in `config.yaml` unter `research:` und lassen sich mit
`make restart` anpassen: Ereigniswörter, Branchenübersetzungen, Zeitfenster,
Sprache und Region. Zu viele ODER-Alternativen verwässern das Ergebnis, deshalb
werden je Gruppe die ersten 14 verwendet.

---

## Zweite Signalquelle: Momentum

Neben PEAD läuft **Cross-Sectional-Momentum** (Jegadeesh/Titman) — getrennt
geführt, mit eigenen Tabellen, eigener Seite und eigener Statistik. Die beiden
Quellen werden **nie zu einer Zahl verrechnet**: sonst wäre bei einem Ergebnis
nie klar, welche Logik es getragen hat.

**So funktioniert es.** Alle Titel werden nach ihrer Rendite der letzten zwölf
Monate rangiert — der jüngste Monat bleibt dabei ausgespart. Das ist kein
Schönheitsfehler, sondern Absicht: auf sehr kurze Sicht neigen Kurse zur
Umkehr, was den Effekt sonst auffrisst. Die Spitzengruppe wird gekauft, die
Schlussgruppe leerverkauft. Umgeschichtet wird beim ersten Pipeline-Lauf eines
Monats.

**Zwei Unterschiede zu PEAD, die man kennen muss:**

- **Momentum ist relativ.** Ein Titel ist nicht „gut", sondern besser als die
  anderen im Universum. In einem fallenden Markt besteht die Long-Gruppe
  komplett aus Verlierern, die nur weniger verloren haben. Ein Momentum von
  −6 % kann Rang 3 bedeuten.
- **Momentum ist kalendergetrieben.** Es äußert sich jeden Monat, nicht nur
  nach einem Ereignis. Anders als bei PEAD ist „keine Aussage" hier die
  Ausnahme.

> **Einschränkung, die hier schwerer wiegt als bei PEAD.** Der dokumentierte
> Faktor rangiert über Hunderte bis Tausende Aktien. Bei 20 US-Large-Caps aus
> wenigen Branchen misst die Rangfolge überwiegend **Branchenrotation** — wenn
> Halbleiter laufen und Konsumgüter nicht, landen die einen oben und die
> anderen unten, ohne dass das etwas mit dem Faktor zu tun hätte. Die Zahlen
> prüfen die Mechanik; sie belegen den Faktor nicht.

### Ausweichquelle: Yahoo springt ein, wo Finnhub nicht darf

Findet die Pipeline einen Titel bei Finnhub gesperrt, holt sie die Earnings
stattdessen über **yfinance** — dieselbe Quelle, die schon die Kurse liefert.
Sie gibt Meldedatum, EPS-Schätzung, tatsächliches EPS und Surprise in *einem*
Abruf, oft über 25 Quartale; Finnhub braucht für weniger zwei Aufrufe. Damit
bekommen auch europäische Titel PEAD-Signale.

Finnhub bleibt trotzdem die erste Wahl, wo es antwortet — nur dort ist der
Meldezeitpunkt (`bmo`/`amc`) belastbar. Yahoo gibt alle Zeitstempel in New
Yorker Zeit aus: bei `AAPL` um 16:00 ET ist „nach Börsenschluss" eindeutig,
bei `SAP.DE` um „20:00 ET" ist der Wert eine Umrechnung, aus der sich die
Frankfurter Meldezeit nicht rekonstruieren lässt. Für nicht-amerikanische
Titel bleibt der Zeitpunkt deshalb **unbekannt** — und die Signal-Logik steigt
dann grundsätzlich erst am Folgetag ein. Das ist in beiden möglichen Lesarten
des Datums sicher: ein zu früher Einstieg, der den Kurssprung vorwegnähme,
ist ausgeschlossen.

Welche Quelle ein Event geliefert hat, steht in `earnings_events.source` und
als Spalte auf `/events`. Abschalten mit `earnings_fallback_enabled: false`.

**Was die Ausweichquelle nicht löst:** yfinance ist ein inoffizieller
Yahoo-Scraper ohne Zusage. Das gilt schon für die Kurse — jetzt hängen beide
Datenarten daran. Und die Abdeckung schwankt: manche Titel liefern 25
Quartale, andere sieben mit Lücken.

### Wenn Finnhub einen Titel nicht abdeckt

Der Free-Tier liefert Earnings praktisch nur für US-gelistete Titel. Bei allen
anderen antwortet Finnhub mit **403** — „Endpunkt für diesen Tarif nicht
freigeschaltet". Das ist keine Störung, sondern eine feste Grenze, und die
Anwendung behandelt sie entsprechend:

- **Der Lauf bleibt `OK`.** Eine bekannte Tarifgrenze ist kein Fehler. Würde
  sie den Lauf auf `PARTIAL` setzen, hieße `PARTIAL` dauerhaft „alles normal"
  und ein echter Fehler ginge darin unter.
- **Die Sperre wird gemerkt** — je Symbol und Endpunkt in `finnhub_coverage`.
  Folgeläufe überspringen diese Abfragen. Bei einem gemischten Universum sind
  das schnell ein paar Dutzend gesparte Requests pro Lauf.
- **Alle `finnhub_recheck_days` (Default 14) wird einmal neu probiert**, damit
  ein Tarif-Upgrade von selbst auffällt. Liefert der Endpunkt wieder Daten,
  verschwindet die Sperre und der Titel läuft normal weiter.
- **Ein Satz statt einer Textwand.** Der Hinweis im Seitenkopf nennt die Anzahl
  betroffener **Titel** (nicht Fehlschläge), die Endpunkte und was trotzdem
  funktioniert: Kurse und Momentum laufen unberührt weiter.

Für diese Titel bleibt der PEAD-Status dauerhaft „Keine Aussage möglich". Wer
das nicht will, nimmt sie aus `config.yaml` heraus.

### Voraussetzung: Kurshistorie

Das Formationsfenster braucht **253 Kurstage je Titel** (252 Handelstage
Rückblick plus einen). Die Rückrechnung für das Diagramm braucht mehr: Jede der
zwölf Monatsrangfolgen braucht selbst zwölf Monate Vorlauf. Deshalb steht
`price_backfill_days` auf 800. Der Start prüft das Minimum und verweigert sich
mit konkreter Meldung, wenn es nicht reicht.

Nachladen ist nicht mehr nötig: Je Titel merkt sich die Datenbank, wie tief
schon geladen wurde (`tickers.price_history_days`). Liegt die Einstellung
darüber — neuer Titel oder erhöhter Wert —, holt der nächste Lauf die Tiefe
einmal nach. `make backfill` bleibt für den Fall, dass man es sofort will.

Die `/momentum`-Seite sagt von sich aus, wenn die Historie nicht reicht:
wie viele Kurstage nötig sind, wie viele Titel bereits versorgt sind und
welche am kürzesten sind.

### Parameter

| Parameter | Default | Bedeutung |
|---|---|---|
| `momentum.enabled` | `true` | Ganz abschaltbar |
| `momentum.lookback_days` | `252` | Formationsfenster in Handelstagen (~12 Monate) |
| `momentum.skip_days` | `21` | Ausgesparte jüngste Handelstage |
| `momentum.group_fraction` | `0.3` | Anteil je Gruppe (0.3 = Terzile, 0.2 = Quintile) |
| `momentum.holding_period_days` | `21` | Haltedauer bis zur Umschichtung |
| `momentum.min_universe` | `8` | Darunter wird nicht rangiert |
| `momentum.rebalance` | `monthly` | Erster Lauf des Monats schichtet um |

Die Umschichtung ist über einen Periodenschlüssel (`2026-09`) idempotent —
mehrere Läufe im selben Monat erzeugen keinen zweiten Korb.

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
die Haltedauer fortgeschritten ist und wo der Kurs gerade steht — und den
[Rechenweg](#warum-dieses-signal-der-rechenweg), der jede Zahl auf ihre
Herkunft zurückführt. Nachvollziehbarkeit ersetzt die Prognose nicht, aber
sie ist das, was sich aus den Daten ehrlich sagen lässt.

### Live und rückwirkend

Ein Signal ist **live**, wenn es spätestens fünf Tage nach der Meldung
entstanden ist — rechtzeitig, um danach zu handeln. Alles später Entstandene ist
**rückwirkend berechnet**: die Signale aus `make seed`, der einmalige
Jahresrückblick für jeden neu aufgenommenen Titel, und die Momentum-Rangfolgen
der letzten zwölf Monate vor dem ersten Live-Korb.

Die Unterscheidung wird aus dem Anlagezeitpunkt abgeleitet
(`models.Signal.retro`), nicht gespeichert — sie gilt damit auch für Signale aus
der Zeit davor. Momentum rückwirkend wird bei jedem Seitenaufruf aus den
gespeicherten Kursen neu berechnet (`app/retro.py`): für jeden Monatsanfang nur
mit den Kursen bis zu diesem Tag, mit dem heutigen Universum, und nie
gespeichert — so bleibt es aktuell, wenn Titel hinzukommen oder wegfallen.

Rückwirkende Zahlen stehen überall **getrennt** unter den Live-Zahlen und zählen
nie in die Kacheln. Drei Gründe, warum sie besser aussehen, als sie damals
gewesen wären:

- **Die Titelauswahl kennt die Vergangenheit.** Wer seine Titel heute wählt,
  hat die großen Verlierer des letzten Jahres eher weggelassen.
- **Die Daten sind nachträglich korrigiert.** Konsensschätzungen und
  gemeldete Gewinne werden revidiert, Kurse um Splits und Dividenden bereinigt.
- **Nichts davon wurde gehandelt.** Keine Gebühren, kein Spread, kein Zögern.

Die Live-Zahl ist die belastbare, die rückwirkende eine grobe Orientierung —
und oft eine ernüchternde: Wenn eine Regel schon rückwirkend kaum besser als
ein Münzwurf abschneidet, wird sie es live erst recht nicht.

### Warum der Kurskontext keine Prognose ist

Gleitende Durchschnitte, 52-Wochen-Spanne und Volatilität stehen auf der
Detailseite als **Lesehilfe mit Beleglage**, nicht als Signal. Keine davon löst
etwas aus. Der Unterschied ist nicht kosmetisch:

- **Richtung ist kaum vorhersagbar.** Klassische Chartindikatoren (Kreuzungen
  gleitender Durchschnitte, RSI, MACD) sind seit Jahrzehnten untersucht. Was
  im Rückblick funktioniert, überlebt den Wechsel auf unbekannte Daten meist
  nicht — und nach Gebühren und Spread erst recht nicht.
- **Schwankung dagegen schon.** Volatilität ist beharrlich: ruhige Phasen
  folgen auf ruhige, turbulente auf turbulente. Deshalb steht sie hier — sie
  sagt, wie viel Bewegung bei diesem Titel normal ist, und macht eine Rendite
  von 3 % einordenbar.
- **Dokumentierte Anomalien sind die Ausnahme.** PEAD ist eine davon, ebenso
  Cross-Sectional-Momentum. Sie sind über Jahrzehnte und Märkte repliziert —
  und genau deshalb implementiert diese Anwendung PEAD und nicht „der Kurs hat
  den 50-Tage-Schnitt gekreuzt".

Genau deshalb ist die zweite Signalquelle in dieser Anwendung Momentum und
nicht „der Kurs hat den 50-Tage-Schnitt gekreuzt" — und genau deshalb steht sie
getrennt (siehe oben).

---

## Wie die Pipeline arbeitet

Ein Durchlauf (`pipeline.run_daily()`), identisch ob vom Scheduler, vom Button,
nach einer Änderung der Titelliste oder von der CLI ausgelöst:

0. **Stammdaten** — einmalig je Ticker: Firmenname, Branche und Börse über
   Finnhub `/stock/profile2`, nur für Symbole, bei denen sie noch fehlen. Danach
   wird der Schritt übersprungen. Schlägt er fehl, fällt die Anzeige auf das
   Symbol zurück und der Rest des Laufs geht weiter.
0b. **Ausblick** — nächster Meldetermin und Analystenverteilung, nur wenn der
   letzte Abruf älter als `outlook_max_age_days` ist oder der gemerkte Termin
   verstrichen. Beides ändert sich langsam; täglich abzufragen kostete zwei
   zusätzliche Requests je Ticker ohne Erkenntnisgewinn.
1. **Earnings abrufen** — Finnhub `/calendar/earnings` für das Rückblickfenster,
   `/stock/earnings` für die Surprise-Historie je Titel. Für jeden Titel einmal
   ein Jahr zurück (400 Tage, `tickers.earnings_history_days` merkt es sich);
   die daraus entstehenden Signale sind rückwirkend. Der SUE einer alten Meldung
   rechnet dabei nur mit den Quartalen davor — nie mit Wissen aus der Zukunft.
2. **Events verarbeiten** — `surprise_pct = (actual − estimate) / |estimate|`.
   Liegen ≥ `min_history_for_sue` Vorquartale vor:
   `sue = surprise_pct / stdev(historische surprise_pct)`.
3. **Signal-Entscheidung** — SUE hat Vorrang; ohne SUE greift die
   `surprise_pct`-Schwelle. Schwellen sind exklusiv (genau auf der Grenze → kein Signal).
4. **Kursdaten** — yfinance, Tages-OHLCV, split- und dividendenbereinigt. Das
   Fenster wird **je Titel** bestimmt: wer weniger als 60 Kurstage hat (etwa
   frisch aufgenommen), bekommt `price_backfill_days`, alle anderen nur
   `price_refresh_days`. Kurse holt der Lauf für alle beobachteten Titel **und**
   für entfernte, auf denen noch eine Position offen ist — sonst schlösse sie
   nie. Vor 22:15 Uhr wird der Kurs des laufenden Tages nicht übernommen: Yahoo
   liefert dann einen Zwischenstand, und eine damit geschlossene Position
   behielte eine Rendite, die es zum Handelsschluss nie gab.
5. **Neue Signale anlegen** — Status `OPEN`, Einstieg am ersten **handelbaren**
   Schlusskurs nach der Meldung.
6. **Offene Positionen prüfen** — sind `holding_period_days` Handelstage seit dem
   Einstieg vergangen, wird geschlossen und `return_pct` berechnet. Fehlt der
   Einstiegskurs noch, wird er hier nachgetragen.
7. **Momentum** — fällige Momentum-Positionen schließen und, falls in diesem
   Monat noch nicht geschehen, den Korb neu zusammenstellen. Läuft nur bei
   `momentum.enabled: true` und berührt die PEAD-Tabellen nicht.

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
Spalten ohne `NULL` bekommen dabei den Standardwert aus dem Modell für die
bestehenden Zeilen; lässt sich eine Spalte nicht gefahrlos nachtragen, bricht
der Start mit einer lesbaren Meldung ab, statt später bei jeder Abfrage mit
`no such column` zu scheitern. Geänderte Typen und neue Constraints deckt das
nicht ab — dafür bleibt `make reset`.

```
app/
├── main.py              FastAPI-App, Routen, Lifespan, Middleware
├── config.py            config.yaml + Env, mit Validierung
├── models.py            SQLAlchemy-Modelle
├── db.py                Engine, Session, Schema-Ergänzung
├── watchlist.py         Titelliste: Startliste, aufnehmen, entfernen, Export
├── watchlist_view.py    Rückmeldungen und Vorauswahl der Titelverwaltung
├── symbols.py           Symbolprüfung: Eingabe -> Unternehmen -> Handelsplatz
├── access.py            IP-Allowlist
├── csrf.py              Herkunfts- und Token-Prüfung für Formulare
├── signals.py           PEAD-Signal-Logik (netz- und DB-frei, voll getestet)
├── explain.py           Rechenweg eines Signals in Alltagssprache
├── momentum.py          Momentum-Logik: Score, Rangfolge, Gruppenbildung
├── momentum_view.py     Aufbereitung der Momentum-Seite
├── stats.py             Konfidenzintervalle, Sperre kleiner Stichproben
├── indicators.py        beschreibende Kurskennzahlen (keine Prognose)
├── charting.py          Diagrammgeometrie (reine Rechnung, kein Rendering)
├── research.py          Recherche-Links mit eingegrenzten Suchanfragen
├── dashboard.py         Übersichtsseite
├── ticker_view.py       Zusammenstellung des Titel-Steckbriefs
├── market_context.py    Kurse je Anfrage einmal laden statt je Zeile
├── coverage.py          gemerkte Finnhub-Tarifsperren
├── pipeline.py          Orchestrierung des Laufs, Hintergrundlauf
├── scheduler.py         APScheduler
├── views.py             Aufbereitung für Frontend und API
├── cli.py               run / check / status / seed / backfill
└── sources/
    ├── finnhub_client.py
    ├── earnings.py      quellenneutrales Earnings-Format
    ├── yahoo_earnings.py  Ausweichquelle für Earnings und Termine
    └── prices.py        yfinance
```

### Datenmodell

| Tabelle | Schlüssel | Inhalt |
|---|---|---|
| `finnhub_coverage` | `id`, unique `(symbol, endpoint)` | Gemerkte Tarifsperren, damit aussichtslose Abfragen entfallen |
| `tickers` | `symbol` | Beobachtete Titel, `active` statt Löschen, Firmenname/Branche/Börse, nächster Meldetermin |
| `analyst_recommendations` | `id`, unique `(symbol, period)` | Verteilung Kaufen/Halten/Verkaufen je Monat |
| `momentum_rebalances` | `id`, unique `period_key` | Eine Umschichtung: Formationsfenster, Universumsgröße |
| `momentum_signals` | `id`, unique `(rebalance_id, symbol)` | Position aus dem Korb, getrennt von `signals` |
| `earnings_events` | `id`, unique `(symbol, report_date)` | EPS, `surprise_pct`, `sue`, `report_hour`, `source`, `processed` |
| `prices` | `(symbol, date)` | Tages-OHLCV, bereinigt |
| `signals` | `id`, unique `earnings_event_id` | Ein- und Ausstieg, Status, `return_pct` |
| `pipeline_runs` | `id` | Protokoll je Lauf, Basis der Statusanzeige |

---

## Tests

```bash
make test
```

Rund 380 Tests, ohne Netzzugriff — `tests/conftest.py` ersetzt Yahoo durch eine
Attrappe, die sich wie bei einem unbekannten Symbol verhält, damit kein Test
stillschweigend davon abhängt, was Yahoo gerade antwortet. Die wichtigsten:

* `tests/test_signals.py` — Surprise, SUE, Schwellenwerte, Short-Rendite,
  Handelstags-Arithmetik über Wochenenden, Einstiegstag je Meldezeitpunkt.
* `tests/test_pipeline.py` — kompletter Durchlauf gegen Attrappen von Finnhub und
  yfinance: Event erfassen, Signal eröffnen, Position schließen, Idempotenz,
  Ausfall einer Datenquelle, Kursfenster je Titel, Zwischenkurse während des
  Handels, Hintergrundlauf nach einer Listenänderung.
* `tests/test_symbols.py` — Symbolprüfung an nachgebildeten Yahoo-Antworten,
  darunter AOMD (drei Firmen, eine Buchstabenfolge).
* `tests/test_retro.py` — Momentum-Rückrechnung, vor allem: die Rangfolge eines
  Monats darf sich nicht ändern, wenn sich Kurse danach ändern.
* `tests/test_herkunft.py` — welche Signale live und welche rückwirkend zählen,
  in Python und in SQL gleich.
* `tests/test_reading_guide.py` — Lesarten je Wert, Reihenfolge nach Beleglage.
* `tests/test_titel_routes.py` — Titelverwaltung über echtes Routing,
  einschließlich abgewiesener Formulare ohne Token oder von fremder Herkunft.

---

## Fehlersuche

| Symptom | Ursache und Abhilfe |
|---|---|
| Container startet nicht, Log zeigt „START ABGEBROCHEN" | `FINNHUB_API_KEY` fehlt in `.env`. |
| `make check` meldet 401 | Key ungültig oder nicht übernommen. In `.env` korrigieren und `make up` (nicht `restart`), dann mit `make env` gegenprüfen. |
| `make check` meldet `/calendar/earnings` nicht verfügbar | Endpunkt im Tarif gesperrt. Die Pipeline weicht auf die Fiskalperiode aus — das Meldedatum ist dann eine Näherung. |
| Lauf endet `PARTIAL` | Eine Quelle ist ausgefallen, die andere lief durch. Ursache im Footer und in `make logs`. |
| Grünes Banner „Lauf abgeschlossen", aber die Seite bleibt leer | Normal, kein Fehler. Es entstehen nur Signale, wenn im Rückblickfenster von 3 Tagen gemeldet **und** die Schwelle überschritten wurde. Außerhalb der Berichtssaison passiert beides nicht. `make status` zeigt `events_count`, `/events` die Rohdaten. Mit `make seed` die letzte Saison nachholen. |
| `/momentum` sagt „Noch keine Rangfolge möglich" | Die Kurshistorie reicht für das Formationsfenster nicht. `make backfill` lädt sie nach. Bleibt es dabei, ist `price_backfill_days` zu klein oder Yahoo liefert für die Symbole keine so lange Historie. |
| Momentum-Korb enthält nur Verlierer | Kein Fehler, sondern die Natur eines relativen Signals: in einem fallenden Markt besteht die Spitzengruppe aus den kleinsten Verlusten. |
| Viele 403-Meldungen beim ersten Lauf | Erwartbar bei nicht-US-Titeln: der Free-Tier deckt sie nicht ab. Ab dem zweiten Lauf werden die Abfragen übersprungen und der Hinweis auf einen Satz verdichtet. Der Lauf bleibt `OK`. |
| Lauf endet `PARTIAL` mit Momentum-Meldung | Der Korb konnte nicht gebildet werden, meist zu kurze Historie. PEAD läuft davon unberührt weiter. |
| Steckbrief zeigt „Keine Aussage möglich" | Kein Fehler. Außerhalb des Drift-Fensters nach einer Gewinnüberraschung hat die PEAD-Logik zu einem Titel nichts zu sagen. Der Termin der nächsten Zahlen steht auf derselben Seite. |
| Kein Analystenbild, kein nächster Termin | `/stock/recommendation` bzw. `/calendar/earnings` sind im Tarif gesperrt oder liefern für diesen Titel nichts. `make check` zeigt den Kalender; die Seite blendet den Block dann aus. |
| Recherche-Links treffen das Falsche | Begriffe in `config.yaml` unter `research:` anpassen, `make restart`. Häufigste Ursachen: fehlende Branchenübersetzung (die englische Bezeichnung wird dann roh gesucht) oder ein zu generischer Firmenname. |
| `make seed` findet trotzdem nichts | Fenster vergrößern (`make seed DAYS=400`); oder `/calendar/earnings` ist im Tarif gesperrt (`make check` zeigt es); oder die Ticker sind keine US-Titel. |
| Titel zeigt „keine Kurse" | Yahoo kennt die Schreibweise nicht. Auf dem Steckbrief „Nicht mehr beobachten", dann unter `/titel` über „Prüfen" neu aufnehmen — die Prüfung bietet nur Listings mit Kursen an. |
| Momentum-Spur beginnt erst mitten im Jahr | Vorher reicht die Kurshistorie nicht für zwölf Monate Vorlauf. Mit `price_backfill_days: 800` holt der nächste Lauf sie nach; die Tabelle unter dem Diagramm nennt den ersten möglichen Monat. |
| Keine rückwirkenden PEAD-Phasen | Der einmalige Jahresrückblick lief noch nicht fehlerfrei durch (Log: „Earnings-Rueckblick"). Er wiederholt sich beim nächsten Lauf. Bei Nicht-US-Titeln liefert Yahoo die Meldungen, ohne Meldezeitpunkt. |
| Titel steht auf „wird geladen …" | Der Hintergrundlauf ist noch nicht fertig; die Seite lädt sich alle zehn Sekunden neu. Dauert es länger als ein paar Minuten, zeigt `make logs` den Grund. |
| „Die Änderung wurde nicht ausgeführt – Formular abgelaufen" | Der Container wurde neu gestartet, seit die Seite geladen wurde. Neu laden und noch einmal abschicken. |
| Änderungen an `tickers` in der `config.yaml` wirken nicht | Gewollt: die Liste wird unter `/titel` gepflegt. Die Datei ist nur die Startliste einer leeren Datenbank; das Log sagt es beim Start. |
| Jede Seite antwortet mit „Internal Server Error" | Im Log steht meist `no such column`. Die Datenbank ist älter als der Code. Ab dieser Fassung ergänzt der Start fehlende Spalten selbst — also `git pull && make rebuild`. Bleibt es dabei, hilft `make reset` (Kurse holt `make backfill` zurück). |
| `./data/poc.db` gehört root | `APP_UID`/`APP_GID` in `.env` auf die eigene ID setzen, `make up`. |
| Port 8000 belegt | `HOST_PORT` in `.env` ändern, `make up`. |

Die SQLite-Datei liegt auf dem Host unter `./data/poc.db` und übersteht
Neustarts wie Rebuilds. `make reset` löscht sie — danach beginnt das
Forward-Tracking von vorn.
