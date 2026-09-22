"""Zugriffsbeschränkung nach Absender-IP.

Warum in der Anwendung und nicht per Firewall: Docker veröffentlicht Ports
über eigene iptables-Ketten, an denen die üblichen ufw-Regeln der
INPUT-Kette vorbeilaufen. Eine Regel, die man dort einträgt, greift für
veröffentlichte Container-Ports oft schlicht nicht - ein bekannter Fallstrick,
der zu einem Dienst führt, den man für geschützt hält.

Entschieden wird ausschließlich anhand der Adresse der TCP-Verbindung.
``X-Forwarded-For`` bleibt bewusst unberücksichtigt: ohne vorgeschalteten
Reverse-Proxy kann diesen Kopf jeder Absender selbst setzen, und wer ihm
traut, hebt die Sperre damit auf.

Frei von Datenbank- und Netzzugriffen (siehe tests/test_access.py).
"""

from __future__ import annotations

import ipaddress
import logging

log = logging.getLogger(__name__)

# Der Healthcheck läuft im Container selbst - ohne diese Ausnahme meldete
# Docker den Container dauerhaft als ungesund.
LOOPBACK = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
)

Network = ipaddress._BaseNetwork


class AccessConfigError(ValueError):
    """Die Allowlist ist unbrauchbar - der Start soll abbrechen."""


def parse_allowlist(raw: str | None) -> tuple[Network, ...]:
    """Liest ALLOWED_IPS: Einzeladressen und CIDR-Bereiche, komma-getrennt.

    Eine Einzeladresse wird als /32 bzw. /128 behandelt. Ein unbrauchbarer
    Eintrag bricht ab, statt stillschweigend zu entfallen - sonst wäre die
    Sperre weiter offen als gedacht.
    """
    networks: list[Network] = []
    for teil in (raw or "").replace(";", ",").split(","):
        eintrag = teil.strip()
        if not eintrag:
            continue
        try:
            networks.append(ipaddress.ip_network(eintrag, strict=False))
        except ValueError as exc:
            raise AccessConfigError(
                f"'{eintrag}' ist weder eine IP-Adresse noch ein CIDR-Bereich: {exc}"
            ) from exc
    return tuple(networks)


def is_allowed(client_ip: str | None, networks: tuple[Network, ...]) -> bool:
    """Darf diese Adresse zugreifen?

    Ohne erkennbare Adresse wird abgelehnt. Loopback ist immer erlaubt,
    damit der Healthcheck im Container funktioniert.
    """
    if not client_ip:
        return False
    try:
        adresse = ipaddress.ip_address(client_ip)
    except ValueError:
        return False

    if any(adresse in netz for netz in LOOPBACK):
        return True
    # Leere Allowlist heisst gesperrt, nicht offen. Der Start verhindert
    # diesen Zustand ohnehin (siehe entrypoint.sh).
    return any(adresse in netz for netz in networks)


LOOPBACK_BINDS = {"127.0.0.1", "localhost", "::1", ""}


def enforcement_needed(bind_addr: str | None) -> bool:
    """Muss die Allowlist überhaupt greifen?

    Ist der Port nur auf das Loopback-Interface des Servers veröffentlicht,
    beschränkt bereits das Betriebssystem den Zugriff - dann darf die
    Middleware nicht zusätzlich filtern. Sie würde sonst alles abweisen:
    Docker übersetzt den veröffentlichten Port, und der Container sieht als
    Absender die Gateway-Adresse des Docker-Netzes, nicht Loopback. Der
    Zugriff über den SSH-Tunnel liefe damit ins Leere.
    """
    return (bind_addr or "").strip() not in LOOPBACK_BINDS


def describe(networks: tuple[Network, ...]) -> str:
    if not networks:
        return "keine (nur localhost)"
    return ", ".join(str(n) for n in networks)
