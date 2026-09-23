"""Schutz der schreibenden Formulare gegen Cross-Site Request Forgery.

Die IP-Sperre hilft hier nicht: Oeffnest du im selben Browser eine fremde
Seite, kann sie ein Formular an diese Anwendung schicken - und die Anfrage
kommt dann von deiner erlaubten Adresse, mit deinem Browser.

Zwei unabhaengige Schranken:

1. **Herkunft.** Browser setzen bei schreibenden Anfragen den Kopf ``Origin``
   (ersatzweise ``Referer``). Er muss zum ``Host`` passen. Fehlen beide,
   ist der Absender kein Browser - etwa curl -, und CSRF setzt einen Browser
   voraus.
2. **Token.** Jedes Formular traegt einen Wert, den eine fremde Seite nicht
   lesen kann (Same-Origin-Policy). Er gilt fuer die Laufzeit des Prozesses;
   nach einem Neustart meldet ein altes Formular sich als abgelaufen.
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Mapping
from urllib.parse import parse_qs, urlsplit

FIELD = "csrf"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Die Formulare dieser Anwendung sind winzig; alles darueber ist kein Formular von hier.
MAX_FORM_BYTES = 16 * 1024

# Gilt fuer diesen Prozess. Die Anwendung laeuft mit einem einzigen
# uvicorn-Worker; mit mehreren muessten sie sich das Geheimnis teilen,
# sonst lehnte jeder Worker die Formulare der anderen ab.
_SECRET = secrets.token_urlsafe(32)


class FormTooLarge(ValueError):
    pass


def token() -> str:
    return _SECRET


def token_ok(submitted: str | None) -> bool:
    return bool(submitted) and hmac.compare_digest(submitted, _SECRET)


def _netloc(value: str) -> str:
    """host[:port] in Kleinschreibung, Standardports weggelassen.

    Der Host-Kopf traegt kein Schema; dort gelten 80 und 443 beide als Standard.
    """
    parts = urlsplit(value if "//" in value else f"//{value}")
    host = (parts.hostname or "").lower()
    port = parts.port
    default = {"http": {80}, "https": {443}}.get(parts.scheme, {80, 443})
    if port is None or port in default:
        return host
    return f"{host}:{port}"


def origin_ok(method: str, headers: Mapping[str, str]) -> bool:
    """Stammt eine schreibende Anfrage von einer Seite dieser Anwendung?"""
    if method.upper() not in UNSAFE_METHODS:
        return True
    host = headers.get("host")
    if not host:
        return False
    expected = _netloc(host)

    origin = headers.get("origin")
    if origin is not None:
        # 'null' schicken Sandbox-iframes und manche Weiterleitungen -
        # nicht zuordenbar, also nicht vertrauenswuerdig.
        return origin != "null" and _netloc(origin) == expected

    referer = headers.get("referer")
    if referer:
        return _netloc(referer) == expected
    return True


def parse_form(body: bytes) -> dict[str, list[str]]:
    """application/x-www-form-urlencoded, ohne Zusatzbibliothek."""
    if len(body) > MAX_FORM_BYTES:
        raise FormTooLarge(f"Formular groesser als {MAX_FORM_BYTES} Bytes")
    return parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=False)
