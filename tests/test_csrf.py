"""CSRF: Herkunftspruefung und Formular-Token."""
import pytest

from app import csrf


@pytest.mark.parametrize("headers", [
    {"host": "localhost:8000", "origin": "http://localhost:8000"},
    {"host": "localhost:9000", "origin": "http://localhost:9000"},   # SSH-Tunnel, anderer Port
    {"host": "203.0.113.7:8000", "origin": "http://203.0.113.7:8000"},
    {"host": "example.org", "origin": "https://example.org"},
    {"host": "Example.org:443", "origin": "https://example.org"},
    {"host": "localhost:8000", "referer": "http://localhost:8000/titel?q=x"},
    {"host": "localhost:8000"},                                        # curl
])
def test_eigene_herkunft_ist_erlaubt(headers):
    assert csrf.origin_ok("POST", headers)


@pytest.mark.parametrize("headers", [
    {"host": "localhost:8000", "origin": "https://evil.example"},
    {"host": "localhost:8000", "origin": "http://localhost:8001"},
    {"host": "localhost:8000", "origin": "null"},
    {"host": "localhost:8000", "referer": "https://evil.example/x"},
    {"origin": "http://localhost:8000"},                              # ohne Host
    # Origin geht vor Referer: ein passender Referer rettet keinen fremden Origin.
    {"host": "localhost:8000", "origin": "https://evil.example",
     "referer": "http://localhost:8000/"},
])
def test_fremde_herkunft_wird_abgewiesen(headers):
    assert not csrf.origin_ok("POST", headers)


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_lesende_anfragen_sind_nie_betroffen(method):
    assert csrf.origin_ok(method, {"host": "a", "origin": "https://evil.example"})


def test_token():
    assert csrf.token_ok(csrf.token())
    assert not csrf.token_ok("falsch")
    assert not csrf.token_ok("")
    assert not csrf.token_ok(None)


def test_formular_parsen():
    form = csrf.parse_form(b"csrf=abc&symbol=AOMD.DE&symbol=NVDA&leer=")
    assert form == {"csrf": ["abc"], "symbol": ["AOMD.DE", "NVDA"]}


def test_uebergrosses_formular():
    with pytest.raises(csrf.FormTooLarge):
        csrf.parse_form(b"x=" + b"a" * (csrf.MAX_FORM_BYTES + 1))
