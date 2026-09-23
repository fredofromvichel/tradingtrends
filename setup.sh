#!/usr/bin/env bash
# ===========================================================================
# PEAD-Signal-POC - Einrichtung auf einem Debian-Server
#
#   ./setup.sh                  interaktiv: prueft Docker, legt .env an, startet
#   ./setup.sh --install-docker installiert Docker vorher, falls es fehlt
#   ./setup.sh --no-start       nur vorbereiten, nicht starten
#   FINNHUB_API_KEY=xyz ./setup.sh   Key ohne Rueckfrage uebernehmen
# ===========================================================================
set -euo pipefail

cd "$(dirname "$0")"

INSTALL_DOCKER=0
START=1
for arg in "$@"; do
    case "$arg" in
        --install-docker) INSTALL_DOCKER=1 ;;
        --no-start)       START=0 ;;
        -h|--help)        sed -n '2,10p' "$0"; exit 0 ;;
        *) echo "Unbekannte Option: $arg" >&2; exit 2 ;;
    esac
done

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mFEHLER:\033[0m %s\n' "$*" >&2; exit 1; }

# --- 1. Docker ------------------------------------------------------------

need_sudo() {
    if [ "$(id -u)" -eq 0 ]; then echo ""; else echo "sudo"; fi
}
SUDO="$(need_sudo)"

if ! command -v docker >/dev/null 2>&1; then
    if [ "$INSTALL_DOCKER" -eq 1 ]; then
        info "Docker nicht gefunden - installiere ueber get.docker.com ..."
        if ! command -v curl >/dev/null 2>&1; then
            $SUDO apt-get update -qq
            $SUDO apt-get install -y -qq curl
        fi
        curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
        $SUDO sh /tmp/get-docker.sh
        rm -f /tmp/get-docker.sh
        if [ "$(id -u)" -ne 0 ]; then
            $SUDO usermod -aG docker "$USER" || true
            warn "Du wurdest der Gruppe 'docker' hinzugefuegt. Damit das greift, einmal"
            warn "ab- und wieder anmelden - oder setup.sh jetzt mit sudo erneut starten."
        fi
    else
        die "Docker ist nicht installiert. Entweder:
    ./setup.sh --install-docker
  oder manuell:
    sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2"
    fi
fi

if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
else
    die "Das Docker-Compose-Plugin fehlt. Installation:
    sudo apt-get install -y docker-compose-v2"
fi

docker info >/dev/null 2>&1 || die "Docker-Daemon nicht erreichbar. Laeuft er? 'sudo systemctl start docker'
Falls Rechte fehlen: 'sudo usermod -aG docker \$USER' und neu anmelden."

info "Docker gefunden: $(docker --version)"
info "Compose gefunden: $($COMPOSE version --short 2>/dev/null || echo 'v1')"

# --- 2. .env --------------------------------------------------------------

if [ ! -f .env ]; then
    cp .env.example .env
    info ".env aus .env.example angelegt."
fi

# UID/GID eintragen, damit ./data/poc.db dem aufrufenden Benutzer gehoert.
CUR_UID="$(id -u)"; CUR_GID="$(id -g)"
sed -i "s/^APP_UID=.*/APP_UID=${CUR_UID}/" .env
sed -i "s/^APP_GID=.*/APP_GID=${CUR_GID}/" .env

get_env() { grep -E "^$1=" .env | head -n1 | cut -d= -f2- | tr -d '"'"'"'' ; }

KEY="$(get_env FINNHUB_API_KEY || true)"
if [ -z "$KEY" ]; then
    KEY="${FINNHUB_API_KEY:-}"
fi
if [ -z "$KEY" ]; then
    echo
    echo "Es wird ein Finnhub-API-Key gebraucht (Free-Tier genuegt)."
    echo "Registrierung: https://finnhub.io/register - der Key steht danach im Dashboard."
    echo
    if [ -t 0 ]; then
        read -r -p "Finnhub API-Key: " KEY
    fi
    [ -n "$KEY" ] || die "Ohne FINNHUB_API_KEY startet die Anwendung nicht.
Key in .env eintragen und setup.sh erneut aufrufen."
    # Key escapen: / und & sind fuer sed bedeutsam.
    ESCAPED="$(printf '%s' "$KEY" | sed -e 's/[\/&]/\\&/g')"
    sed -i "s/^FINNHUB_API_KEY=.*/FINNHUB_API_KEY=${ESCAPED}/" .env
    info "Key in .env gespeichert."
fi
chmod 600 .env

# --- 3. Datenverzeichnis --------------------------------------------------

mkdir -p data
info "Datenverzeichnis: $(pwd)/data (hier landet poc.db)"

# --- 4. Ticker-Hinweis ----------------------------------------------------

if grep -q "PLATZHALTER" config.yaml; then
    warn "config.yaml enthaelt noch die Platzhalter-Tickerliste."
    warn "Vor dem produktiven Einsatz dort die eigenen 20 Symbole eintragen,"
    warn "danach: $COMPOSE restart"
fi

# --- 5. Start -------------------------------------------------------------

if [ "$START" -eq 0 ]; then
    info "Vorbereitung abgeschlossen (--no-start). Start mit: $COMPOSE up -d --build"
    exit 0
fi

info "Baue Image und starte Container ..."
$COMPOSE up -d --build

BIND="$(get_env BIND_ADDR)"; BIND="${BIND:-127.0.0.1}"
PORT="$(get_env HOST_PORT)"; PORT="${PORT:-8000}"

info "Warte auf die Anwendung ..."
for i in $(seq 1 45); do
    if curl -fsS "http://${BIND}:${PORT}/healthz" >/dev/null 2>&1; then
        echo
        info "Bereit."
        echo
        echo "  Lokal auf dem Server : http://${BIND}:${PORT}"
        if [ "$BIND" = "127.0.0.1" ]; then
            echo "  Von deinem Rechner   : ssh -N -L ${PORT}:localhost:${PORT} $(id -un)@$(hostname -f 2>/dev/null || hostname)"
            echo "                         danach http://localhost:${PORT} im Browser"
        fi
        echo
        echo "  Datenquellen pruefen : $COMPOSE exec app python -m app.cli check"
        echo "  Pipeline manuell     : $COMPOSE exec app python -m app.cli run"
        echo "  Logs                 : $COMPOSE logs -f"
        echo
        exit 0
    fi
    sleep 2
done

warn "Die Anwendung hat nach 90 s nicht geantwortet. Logs:"
$COMPOSE logs --tail=40
exit 1
