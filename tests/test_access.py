"""Tests der IP-Zugriffsbeschränkung - reine Entscheidungslogik."""

from __future__ import annotations

import pytest

from app.access import AccessConfigError, describe, is_allowed, parse_allowlist


# -- Allowlist lesen --------------------------------------------------------


def test_einzeladresse():
    netze = parse_allowlist("203.0.113.7")
    assert len(netze) == 1
    assert is_allowed("203.0.113.7", netze) is True
    assert is_allowed("203.0.113.8", netze) is False


def test_mehrere_adressen():
    netze = parse_allowlist("203.0.113.7, 198.51.100.4")
    assert is_allowed("203.0.113.7", netze) is True
    assert is_allowed("198.51.100.4", netze) is True
    assert is_allowed("192.0.2.1", netze) is False


def test_cidr_bereich():
    netze = parse_allowlist("198.51.100.0/24")
    assert is_allowed("198.51.100.1", netze) is True
    assert is_allowed("198.51.100.255", netze) is True
    assert is_allowed("198.51.101.1", netze) is False


def test_semikolon_als_trenner_und_leerraum():
    netze = parse_allowlist(" 203.0.113.7 ; 198.51.100.0/24 ")
    assert len(netze) == 2
    assert is_allowed("198.51.100.9", netze) is True


def test_ipv6():
    netze = parse_allowlist("2001:db8::/32")
    assert is_allowed("2001:db8::1", netze) is True
    assert is_allowed("2001:db9::1", netze) is False


def test_leere_liste():
    assert parse_allowlist("") == ()
    assert parse_allowlist(None) == ()


def test_unbrauchbarer_eintrag_bricht_ab():
    """Stillschweigend zu überspringen ließe die Sperre offener als gedacht."""
    with pytest.raises(AccessConfigError) as exc:
        parse_allowlist("203.0.113.7, kein-hostname")
    assert "kein-hostname" in str(exc.value)


def test_hostnamen_werden_nicht_aufgeloest():
    with pytest.raises(AccessConfigError):
        parse_allowlist("mein-anschluss.dyndns.org")


# -- Entscheidung -----------------------------------------------------------


def test_localhost_ist_immer_erlaubt():
    """Der Healthcheck läuft im Container selbst."""
    assert is_allowed("127.0.0.1", ()) is True
    assert is_allowed("::1", ()) is True
    assert is_allowed("127.0.0.5", parse_allowlist("203.0.113.7")) is True


def test_leere_allowlist_sperrt_alles_ausser_localhost():
    assert is_allowed("203.0.113.7", ()) is False


def test_ohne_erkennbare_adresse_wird_abgelehnt():
    assert is_allowed(None, parse_allowlist("203.0.113.7")) is False
    assert is_allowed("", parse_allowlist("203.0.113.7")) is False
    assert is_allowed("kaputt", parse_allowlist("203.0.113.7")) is False


def test_beschreibung_fuer_die_logzeile():
    assert describe(()) == "keine (nur localhost)"
    assert "203.0.113.7/32" in describe(parse_allowlist("203.0.113.7"))
