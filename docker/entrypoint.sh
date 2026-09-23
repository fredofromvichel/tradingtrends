#!/bin/sh
# Rechte auf dem Daten-Volume geraderuecken, Konfiguration vorpruefen,
# dann unprivilegiert weiterlaufen.
set -e

DB_PATH="${DB_PATH:-/data/poc.db}"
DATA_DIR="$(dirname "$DB_PATH")"
APP_UID="${APP_UID:-1000}"
APP_GID="${APP_GID:-1000}"

fail() {
    echo "" >&2
    echo "============================================================" >&2
    echo " START ABGEBROCHEN" >&2
    echo "------------------------------------------------------------" >&2
    printf ' %s\n' "$@" >&2
    echo "============================================================" >&2
    echo "" >&2
    exit 1
}

if [ "$(id -u)" = "0" ]; then
    # UID/GID des App-Users an den Host anpassen, damit ./data/poc.db dem
    # aufrufenden Benutzer gehoert und nicht root.
    current_uid="$(id -u app)"
    current_gid="$(id -g app)"
    [ "$APP_GID" != "$current_gid" ] && groupmod -o -g "$APP_GID" app
    [ "$APP_UID" != "$current_uid" ] && usermod -o -u "$APP_UID" app

    mkdir -p "$DATA_DIR"
    chown -R app:app "$DATA_DIR" 2>/dev/null || true
    chown -R app:app /app 2>/dev/null || true
fi

if [ -z "${FINNHUB_API_KEY:-}" ]; then
    fail \
      "FINNHUB_API_KEY ist nicht gesetzt." \
      "" \
      "  1. cp .env.example .env" \
      "  2. Key von https://finnhub.io/dashboard in .env eintragen" \
      "  3. docker compose up -d"
fi

if [ ! -f "${CONFIG_PATH:-/app/config.yaml}" ]; then
    fail "config.yaml fehlt unter ${CONFIG_PATH:-/app/config.yaml}."
fi

# Ein auf allen Schnittstellen veroeffentlichter Port ohne Allowlist waere ein
# offener Dienst ohne Authentifizierung. Lieber gar nicht starten.
if [ "${BIND_ADDR:-127.0.0.1}" != "127.0.0.1" ] && [ "${BIND_ADDR:-}" != "localhost" ]; then
    if [ -z "${ALLOWED_IPS:-}" ]; then
        fail \
          "BIND_ADDR ist ${BIND_ADDR} - der Port ist damit im Netz erreichbar," \
          "aber ALLOWED_IPS ist leer. Das waere ein offener Dienst ohne" \
          "Authentifizierung." \
          "" \
          "Entweder in der .env die eigene IP eintragen:" \
          "  ALLOWED_IPS=203.0.113.7" \
          "oder BIND_ADDR wieder auf 127.0.0.1 setzen und den SSH-Tunnel nutzen."
    fi
fi

if [ "$(id -u)" = "0" ]; then
    exec gosu app "$@"
fi
exec "$@"
