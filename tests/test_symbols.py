"""Symbolpruefung: von der Eingabe zu Unternehmen und empfohlenem Listing.

Die Suchergebnisse sind echten Yahoo-Antworten nachgebildet - insbesondere
der Fall AOMD, der einmal beinahe AMD geworden waere und an der NYSE eine
dritte, voellig andere Firma ist.
"""
import datetime as dt

import pytest

from app.symbols import (
    Quote,
    _company_key,
    normalize_symbol,
    resolve,
    resolve_many,
    split_queries,
)

HEUTE = dt.date(2026, 9, 23)

SUCHE = {
    "AOMD": [
        Quote("AOMD", "Angel Oak Mortgage REIT, Inc. 9", "NYQ", "NYSE"),
        Quote("AOMD.MU", "Alstom SA", "MUN", "Munich"),
        Quote("AOMD.DE", "Alstom SA", "GER", "XETRA"),
        Quote("AOMD.F", "Alstom SA", "FRA", "Frankfurt"),
    ],
    "AOMD.DE": [Quote("AOMD.DE", "Alstom SA", "GER", "XETRA")],
    "AOMD.F": [Quote("AOMD.F", "Alstom SA", "FRA", "Frankfurt")],
    "Alstom SA": [
        Quote("ALO.PA", "Alstom SA", "PAR", "Paris"),
        Quote("ALSMY", "Alstom SA", "PNK", "OTC Markets"),
        Quote("AOMD.DE", "Alstom SA", "GER", "XETRA"),
    ],
    "NVD": [
        Quote("NVD", "Graniteshares 2x Short NVDA Daily ETF", "NMS", "NASDAQ", "ETF"),
        Quote("NVD.DE", "NVIDIA Corporation", "GER", "XETRA"),
    ],
    "NVIDIA Corporation": [
        Quote("NVDA", "NVIDIA Corporation", "NMS", "NASDAQ"),
        Quote("NVD.F", "NVIDIA Corporation", "FRA", "Frankfurt"),
        Quote("NVDA.BA", "NVIDIA Corporation", "BUE", "Buenos Aires"),
    ],
    "AIR": [
        Quote("AIR", "AAR Corp.", "NYQ", "NYSE"),
        Quote("AAL", "American Airlines Group Inc.", "NMS", "NASDAQ"),
        Quote("DAL", "Delta Air Lines, Inc.", "NYQ", "NYSE"),
    ],
    "AIR.DE": [Quote("AIR.DE", "Airbus SE", "GER", "XETRA")],
    "BY6": [
        Quote("BY6.F", "BYD Company Limited", "FRA", "Frankfurt"),
        Quote("BY6.SG", "BYD Co Ltd", "STU", "Stuttgart"),
    ],
    "DAU0.F": [Quote("DAU0.F", "Dassault Aviation société anonyme", "FRA", "Frankfurt")],
    "DAU0": [Quote("DAU0.MU", "Dassault Aviation SA", "MUN", "Munich")],
    "Carl Zeiss Meditec": [
        Quote("AFX.F", "Carl Zeiss Meditec AG", "FRA", "Frankfurt"),
        Quote("CZMWY", "Carl Zeiss Meditec AG", "PNK", "OTC Markets"),
        Quote("AFX.DE", "Carl Zeiss Meditec AG", "GER", "XETRA"),
    ],
}


def suche(query):
    return SUCHE.get(query, [])


def kurse(symbols):
    return {s: (HEUTE, 10.0 + i) for i, s in enumerate(symbols)}


def firmen(lookup):
    return [c.name for c in lookup.companies]


# -- Der Fall, um den es geht ------------------------------------------------


def test_aomd_zeigt_beide_firmen_und_waehlt_nichts_vor():
    lk = resolve("AOMD", suche, kurse)
    assert firmen(lk) == ["Angel Oak Mortgage REIT, Inc. 9", "Alstom SA"]
    assert lk.ambiguous
    assert lk.preselected is None


def test_alstom_empfiehlt_xetra_vor_frankfurt_und_paris():
    lk = resolve("AOMD", suche, kurse)
    alstom = lk.companies[1]
    assert [l.symbol for l in alstom.listings][:3] == ["AOMD.DE", "AOMD.F", "ALO.PA"]
    assert alstom.recommended.symbol == "AOMD.DE"
    assert "Xetra" in alstom.reason


def test_otc_und_regionalboerse_landen_hinten():
    lk = resolve("AOMD", suche, kurse)
    codes = [l.exchange_code for l in lk.companies[1].listings]
    assert codes[-1] in {"MUN", "PNK"}


def test_nvd_findet_ueber_den_namen_das_us_listing():
    """Ueber das Kuerzel allein taucht NVDA nicht auf - erst die Namenssuche."""
    lk = resolve("NVD", suche, kurse)
    assert firmen(lk) == ["NVIDIA Corporation"]
    assert lk.preselected == "NVDA"
    assert "Finnhub" in lk.companies[0].reason


def test_etfs_fallen_raus():
    lk = resolve("NVD", suche, kurse)
    symbole = [l.symbol for c in lk.companies for l in c.listings]
    assert "NVD" not in symbole


def test_air_findet_airbus_ueber_die_xetra_probe():
    lk = resolve("AIR", suche, kurse)
    assert "Airbus SE" in firmen(lk)
    assert lk.ambiguous


def test_beifang_der_unscharfen_suche_faellt_weg():
    """'American Airlines' enthaelt 'air' nur als Wortteil."""
    lk = resolve("AIR", suche, kurse)
    assert "American Airlines Group Inc." not in firmen(lk)


def test_symboltreffer_stehen_vor_namenstreffern():
    lk = resolve("AIR", suche, kurse)
    assert firmen(lk).index("Delta Air Lines, Inc.") > firmen(lk).index("Airbus SE")


def test_schreibvarianten_derselben_firma_werden_zusammengefasst():
    lk = resolve("BY6", suche, kurse)
    assert len(lk.companies) == 1
    assert lk.preselected == "BY6.F"


def test_franzoesische_rechtsform_mit_akzent():
    lk = resolve("DAU0", suche, kurse)
    assert len(lk.companies) == 1


def test_eindeutiger_name_waehlt_xetra_vor():
    lk = resolve("Carl Zeiss Meditec", suche, kurse)
    assert lk.preselected == "AFX.DE"


# -- Randfaelle ---------------------------------------------------------------


def test_listing_ohne_kurs_wird_nicht_angeboten():
    def nur_xetra(symbols):
        return {s: (HEUTE, 15.4) for s in symbols if s == "AOMD.DE"}

    lk = resolve("AOMD", suche, nur_xetra)
    assert firmen(lk) == ["Alstom SA"]
    assert [l.symbol for l in lk.companies[0].listings] == ["AOMD.DE"]


def test_letzter_kurs_steht_am_listing():
    lk = resolve("Carl Zeiss Meditec", suche, kurse)
    best = lk.companies[0].recommended
    assert best.last_close is not None and best.last_date == HEUTE


def test_ausfall_der_kursprobe_verschweigt_die_treffer_nicht():
    def kaputt(_symbols):
        raise RuntimeError("Yahoo down")

    lk = resolve("Carl Zeiss Meditec", suche, kaputt)
    assert lk.companies and lk.companies[0].recommended.last_close is None


def test_ausfall_der_suche_meldet_sich_lesbar():
    def kaputt(_q):
        raise RuntimeError("Timeout")

    lk = resolve("AOMD", kaputt, kurse)
    assert lk.companies == [] and "nicht erreichbar" in lk.error


def test_nichts_gefunden():
    lk = resolve("XXXXQ", suche, kurse)
    assert lk.companies == [] and lk.error is None


def test_mehrere_eingaben():
    ergebnisse = resolve_many("AOMD, NVD; Carl Zeiss Meditec", suche, kurse)
    assert [lk.query for lk in ergebnisse] == ["AOMD", "NVD", "Carl Zeiss Meditec"]


@pytest.mark.parametrize("roh,erwartet", [
    ("AOMD, NVD", ["AOMD", "NVD"]),
    ("Carl Zeiss Meditec", ["Carl Zeiss Meditec"]),       # Leerzeichen trennt nicht
    ("SAP;\nsap , ,", ["SAP"]),                            # Dubletten, Leereintraege
    ("", []),
])
def test_eingabe_zerlegen(roh, erwartet):
    assert split_queries(roh) == erwartet


def test_hoechstens_zehn_eingaben():
    assert len(split_queries(",".join(f"T{i}" for i in range(30)))) == 10


@pytest.mark.parametrize("roh,erwartet", [
    ("aomd.de", "AOMD.DE"),
    (" BRK-B ", "BRK-B"),
    ("^GDAXI", None),            # Index, faengt nicht mit Buchstabe/Ziffer an
    ("AAPL; DROP", None),
    ("", None),
])
def test_symbol_normalisieren(roh, erwartet):
    assert normalize_symbol(roh) == erwartet


@pytest.mark.parametrize("a,b", [
    ("BYD Company Limited", "BYD Co Ltd"),
    ("Dassault Aviation société anonyme", "Dassault Aviation SA"),
    ("Alibaba Group Holding Limited", "Alibaba Group Holding Ltd."),
])
def test_firmenschluessel_gleich(a, b):
    assert _company_key(a) == _company_key(b)


def test_verwandte_firmen_bleiben_getrennt():
    assert _company_key("Alibaba Group Holding Limited") != _company_key(
        "Alibaba Health Information Technology Limited"
    )
