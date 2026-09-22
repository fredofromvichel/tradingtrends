"""Tests des Ticker-Übernahme-Werkzeugs.

Die Blockerkennung ist der heikle Teil: greift sie zu weit, reißt sie den
nächsten Konfigurationsabschnitt mit heraus.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

_spec = importlib.util.spec_from_file_location(
    "merge_tickers", Path(__file__).resolve().parent.parent / "tools" / "merge-tickers.py"
)
merge_tickers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(merge_tickers)

ALT = """tickers:
  - SAP.DE
  - SIE.DE
  - ALV.DE

sue_threshold_buy: 1.0
"""

NEU = """# Kommentar über den Tickern
tickers:
  - AAPL
  - MSFT

# ---------------------------------------------------------------------------
# Signal-Schwellen
# ---------------------------------------------------------------------------
sue_threshold_buy: 1.0
holding_period_days: 20

momentum:
  enabled: true
"""


def write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_tickerliste_wird_uebernommen(tmp_path: Path):
    alt = write(tmp_path, "alt.yaml", ALT)
    neu = write(tmp_path, "neu.yaml", NEU)

    merge_tickers.main(["x", str(alt), str(neu)])

    data = yaml.safe_load(neu.read_text(encoding="utf-8"))
    assert data["tickers"] == ["SAP.DE", "SIE.DE", "ALV.DE"]


def test_uebrige_konfiguration_bleibt_unberuehrt(tmp_path: Path):
    alt = write(tmp_path, "alt.yaml", ALT)
    neu = write(tmp_path, "neu.yaml", NEU)

    merge_tickers.main(["x", str(alt), str(neu)])

    data = yaml.safe_load(neu.read_text(encoding="utf-8"))
    assert data["holding_period_days"] == 20
    assert data["momentum"] == {"enabled": True}


def test_kommentare_ueberleben(tmp_path: Path):
    """Der ganze Grund, warum das Werkzeug textuell arbeitet."""
    alt = write(tmp_path, "alt.yaml", ALT)
    neu = write(tmp_path, "neu.yaml", NEU)

    merge_tickers.main(["x", str(alt), str(neu)])

    text = neu.read_text(encoding="utf-8")
    assert "# Signal-Schwellen" in text
    assert "# Kommentar über den Tickern" in text


def test_sicherungskopie_wird_angelegt(tmp_path: Path):
    alt = write(tmp_path, "alt.yaml", ALT)
    neu = write(tmp_path, "neu.yaml", NEU)

    merge_tickers.main(["x", str(alt), str(neu)])

    backup = tmp_path / "neu.yaml.bak"
    assert backup.exists()
    assert yaml.safe_load(backup.read_text(encoding="utf-8"))["tickers"] == ["AAPL", "MSFT"]


def test_tickerblock_am_dateiende(tmp_path: Path):
    alt = write(tmp_path, "alt.yaml", "tickers:\n  - NVDA\n")
    neu = write(tmp_path, "neu.yaml", NEU)

    merge_tickers.main(["x", str(alt), str(neu)])

    assert yaml.safe_load(neu.read_text(encoding="utf-8"))["tickers"] == ["NVDA"]


def test_fehlender_block_bricht_ab_statt_zu_zerstoeren(tmp_path: Path):
    alt = write(tmp_path, "alt.yaml", "irgendwas: 1\n")
    neu = write(tmp_path, "neu.yaml", NEU)

    assert merge_tickers.main(["x", str(alt), str(neu)]) == 1
    # Die Zieldatei bleibt unangetastet.
    assert yaml.safe_load(neu.read_text(encoding="utf-8"))["tickers"] == ["AAPL", "MSFT"]


def test_leerer_block_bricht_ab(tmp_path: Path):
    alt = write(tmp_path, "alt.yaml", "tickers:\n\nsue_threshold_buy: 1.0\n")
    neu = write(tmp_path, "neu.yaml", NEU)

    assert merge_tickers.main(["x", str(alt), str(neu)]) == 1
    assert yaml.safe_load(neu.read_text(encoding="utf-8"))["tickers"] == ["AAPL", "MSFT"]


def test_einzeilige_liste_wird_erkannt_und_gemeldet(tmp_path: Path):
    """Inline-Schreibweise kann das Werkzeug nicht - dann lieber sauber abbrechen."""
    alt = write(tmp_path, "alt.yaml", "tickers: [AAPL, MSFT]\n")
    neu = write(tmp_path, "neu.yaml", NEU)

    assert merge_tickers.main(["x", str(alt), str(neu)]) == 1


def test_fehlende_datei_meldet_sich(tmp_path: Path):
    neu = write(tmp_path, "neu.yaml", NEU)
    assert merge_tickers.main(["x", str(tmp_path / "gibtsnicht.yaml"), str(neu)]) == 2


def test_gegen_die_echte_config(tmp_path: Path):
    """Das Werkzeug muss mit der ausgelieferten config.yaml zurechtkommen."""
    echte = Path(__file__).resolve().parent.parent / "config.yaml"
    neu = write(tmp_path, "neu.yaml", echte.read_text(encoding="utf-8"))
    alt = write(tmp_path, "alt.yaml", ALT)

    assert merge_tickers.main(["x", str(alt), str(neu)]) == 0

    data = yaml.safe_load(neu.read_text(encoding="utf-8"))
    assert data["tickers"] == ["SAP.DE", "SIE.DE", "ALV.DE"]
    # Alle Abschnitte der neuen Fassung müssen erhalten sein.
    for key in ("momentum", "research", "schedule", "price_backfill_days",
                "holding_period_days", "sue_threshold_buy"):
        assert key in data, key


def test_bericht_schneidet_inline_kommentare_ab(tmp_path: Path, capsys):
    """Sonst steht der Kommentar im Bericht als Teil des Symbols."""
    alt = write(tmp_path, "alt.yaml", "tickers:\n  - NVDA   # NVD · NVIDIA\n  - SAP.DE\n")
    neu = write(tmp_path, "neu.yaml", NEU)

    merge_tickers.main(["x", str(alt), str(neu)])

    ausgabe = capsys.readouterr().out
    assert "2 Ticker uebernommen: NVDA, SAP.DE" in ausgabe
    # Der Kommentar bleibt in der Datei selbst erhalten.
    assert "# NVD · NVIDIA" in neu.read_text(encoding="utf-8")
