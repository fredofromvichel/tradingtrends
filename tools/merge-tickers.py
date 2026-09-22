#!/usr/bin/env python3
"""Uebernimmt die Tickerliste aus einer alten config.yaml in eine neue.

Gedacht fuer den Fall, dass die eigene Liste erhalten bleiben soll, waehrend
die uebrige Konfiguration auf einen neueren Stand wechselt - etwa nach einem
Branchwechsel oder einem groesseren Update.

    python3 tools/merge-tickers.py ~/config.mein.yaml config.yaml

Arbeitet rein textuell. PyYAML wuerde die Datei beim Neuschreiben zwar korrekt
serialisieren, dabei aber saemtliche Kommentare verlieren - und die sind in
config.yaml die halbe Dokumentation.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


class BlockNotFound(RuntimeError):
    pass


def ticker_block(lines: list[str], source: str) -> tuple[int, int]:
    """Grenzen des tickers-Blocks: (Index der 'tickers:'-Zeile, Index dahinter)."""
    start = None
    for index, line in enumerate(lines):
        if line.rstrip() == "tickers:":
            start = index
            break
        if line.startswith("tickers:") and "[" in line:
            raise BlockNotFound(
                f"{source}: 'tickers' steht als einzeilige Liste da. "
                "Bitte von Hand uebertragen."
            )
    if start is None:
        raise BlockNotFound(f"{source}: kein 'tickers:'-Block gefunden.")

    end = start + 1
    while end < len(lines):
        line = lines[end]
        # Ein Schluessel auf Spalte 0, der kein Kommentar ist, beendet den Block.
        if line.strip() and not line.startswith((" ", "\t", "#")):
            break
        end += 1
    # Nachlaufende Leerzeilen und Kommentare gehoeren zum Folgeabschnitt.
    while end > start + 1 and not lines[end - 1].strip().startswith("-"):
        end -= 1
    return start, end


def merge(old_path: Path, new_path: Path) -> list[str]:
    old = old_path.read_text(encoding="utf-8").splitlines(keepends=True)
    new = new_path.read_text(encoding="utf-8").splitlines(keepends=True)

    old_start, old_end = ticker_block(old, str(old_path))
    new_start, new_end = ticker_block(new, str(new_path))

    tickers = [line for line in old[old_start + 1:old_end] if line.strip().startswith("-")]
    if not tickers:
        raise BlockNotFound(f"{old_path}: der tickers-Block ist leer.")

    return new[:new_start + 1] + tickers + new[new_end:]


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2

    old_path, new_path = Path(argv[1]), Path(argv[2])
    for path in (old_path, new_path):
        if not path.is_file():
            print(f"Nicht gefunden: {path}", file=sys.stderr)
            return 2

    try:
        merged = merge(old_path, new_path)
    except BlockNotFound as exc:
        print(f"Abbruch: {exc}", file=sys.stderr)
        return 1

    backup = new_path.with_suffix(new_path.suffix + ".bak")
    shutil.copy2(new_path, backup)
    new_path.write_text("".join(merged), encoding="utf-8")

    start, end = ticker_block(merged, str(new_path))
    taken = [
        # Inline-Kommentar abschneiden, sonst steht er im Bericht als Teil
        # des Symbols.
        line.strip().lstrip("-").split("#")[0].strip()
        for line in merged[start + 1:end]
        if line.strip().startswith("-")
    ]
    print(f"{len(taken)} Ticker uebernommen: {', '.join(taken)}")
    print(f"Vorherige Fassung gesichert unter: {backup}")
    print("Wirksam nach: make restart")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
