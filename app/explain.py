"""Rechenweg eines Signals in Alltagssprache.

Zweck: aus einer Zahl eine nachvollziehbare Kette machen. Nicht "SUE 1,08,
also BUY", sondern: was das Unternehmen gemeldet hat, was erwartet war, wie
ungewöhnlich die Abweichung für genau dieses Unternehmen ist, warum daraus
ein Signal folgt, wann gekauft wurde und warum an diesem Tag - und am Ende,
ob das Ergebnis besser war als der Rest des Universums.

Jeder Schritt trägt den Rechenweg mit den tatsächlichen Werten, damit sich
jede Zahl auf der Seite zurückverfolgen lässt.

Frei von Datenbank- und Netzzugriffen (siehe tests/test_explain.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Step:
    label: str
    text: str
    # Die Rechnung mit eingesetzten Zahlen, wo es eine gibt.
    formula: str | None = None
    # "neutral" | "good" | "bad" - faerbt nur die Randmarkierung.
    tone: str = "neutral"


@dataclass
class Explanation:
    headline: str
    steps: list[Step] = field(default_factory=list)
    caveat: str | None = None


def _pct(value: float | None, digits: int = 1) -> str:
    return "–" if value is None else f"{value * 100:+.{digits}f} %".replace(".", ",")


def _pct_plain(value: float | None, digits: int = 1) -> str:
    return "–" if value is None else f"{value * 100:.{digits}f} %".replace(".", ",")


def _num(value: float | None, digits: int = 2) -> str:
    return "–" if value is None else f"{value:.{digits}f}".replace(".", ",")


def _tag(value) -> str:
    """Datum in der Schreibweise, die im Fliesstext lesbar ist."""
    return value.strftime("%d.%m.%Y") if hasattr(value, "strftime") else str(value)


def _minus(value: float | None, digits: int = 2) -> str:
    """Wert fuer die rechte Seite einer Subtraktion, ohne doppeltes Vorzeichen."""
    if value is None:
        return "–"
    if value < 0:
        return f"+ {_pct_plain(abs(value), digits)}"
    return f"− {_pct_plain(value, digits)}"


def _hour_text(hour: str | None) -> str:
    return {
        "bmo": "vor Börsenbeginn",
        "amc": "nach Börsenschluss",
        "dmh": "während des Handels",
    }.get(hour or "", "zu unbekannter Uhrzeit")


def explain_pead(signal, event, settings) -> Explanation:
    """Warum dieses PEAD-Signal entstand und was daraus wurde."""
    richtung = "Kauf" if signal.signal_type == "BUY" else "Leerverkauf"
    steps: list[Step] = []

    # 1. Was gemeldet wurde
    if event and event.eps_actual is not None and event.eps_estimate is not None:
        steps.append(Step(
            label="Die Meldung",
            text=(
                f"Am {_tag(signal.trigger_date)} meldete das Unternehmen einen Gewinn von "
                f"{_num(event.eps_actual)} je Aktie. Analysten hatten "
                f"{_num(event.eps_estimate)} erwartet."
            ),
            formula=f"tatsächlich {_num(event.eps_actual)} · erwartet {_num(event.eps_estimate)}",
        ))
    else:
        steps.append(Step(
            label="Die Meldung",
            text=f"Am {_tag(signal.trigger_date)} lag eine Quartalsmeldung vor.",
        ))

    # 2. Die Überraschung
    surprise = signal.surprise_pct_at_signal
    if surprise is not None:
        besser = "über" if surprise > 0 else "unter"
        steps.append(Step(
            label="Die Überraschung",
            text=(
                f"Der Gewinn lag damit {_pct_plain(abs(surprise))} {besser} der Erwartung. "
                "Diese Abweichung ist der Auslöser – nicht der Gewinn selbst."
            ),
            formula=(
                f"({_num(event.eps_actual)} − {_num(event.eps_estimate)}) ÷ "
                f"{_num(abs(event.eps_estimate))} = {_pct(surprise)}"
                if event and event.eps_actual is not None and event.eps_estimate
                else None
            ),
            tone="good" if surprise > 0 else "bad",
        ))

    # 3. Einordnung: wie ungewöhnlich ist das für DIESES Unternehmen?
    sue = signal.sue_at_signal
    if sue is not None and event and event.surprise_stdev:
        steps.append(Step(
            label="Ist das viel?",
            text=(
                f"Dieses Unternehmen weicht üblicherweise um ±{_pct_plain(event.surprise_stdev)} "
                f"von der Erwartung ab (gemessen an {event.history_count or '–'} früheren "
                "Quartalen). Gemessen an seiner eigenen Schwankung ist die Abweichung also "
                f"das {_num(abs(sue))}-fache des Üblichen. Genau das misst der SUE-Wert."
            ),
            formula=(
                f"{_pct(surprise)} ÷ {_pct_plain(event.surprise_stdev)} = SUE {_num(sue)}"
            ),
        ))
    elif surprise is not None:
        steps.append(Step(
            label="Ist das viel?",
            text=(
                "Für dieses Unternehmen liegen noch zu wenige frühere Quartale vor, um die "
                "Abweichung an seiner eigenen Schwankung zu messen. Deshalb zählt hier die "
                "reine Überraschung gegen eine feste Schwelle."
            ),
        ))

    # 4. Warum daraus ein Signal folgt
    if sue is not None:
        schwelle = (
            settings.sue_threshold_buy if signal.signal_type == "BUY"
            else settings.sue_threshold_sell
        )
        vergleich = "über" if signal.signal_type == "BUY" else "unter"
        steps.append(Step(
            label=f"Warum {signal.signal_type}",
            text=(
                f"SUE {_num(sue)} liegt {vergleich} der {richtung}-Schwelle von "
                f"{_num(schwelle)}. Die Erfahrung aus der Forschung: nach einer so "
                "ungewöhnlichen Überraschung läuft der Kurs noch Wochen in dieselbe "
                "Richtung weiter, statt sofort zu springen."
            ),
            formula=f"SUE {_num(sue)} {'>' if signal.signal_type == 'BUY' else '<'} {_num(schwelle)}",
            tone="good" if signal.signal_type == "BUY" else "bad",
        ))
    elif surprise is not None:
        schwelle = (
            settings.surprise_pct_fallback_buy if signal.signal_type == "BUY"
            else settings.surprise_pct_fallback_sell
        )
        steps.append(Step(
            label=f"Warum {signal.signal_type}",
            text=(
                f"Die Überraschung von {_pct(surprise)} liegt jenseits der Ersatzschwelle "
                f"von {_pct(schwelle)}."
            ),
            formula=f"{_pct(surprise)} {'>' if signal.signal_type == 'BUY' else '<'} {_pct(schwelle)}",
        ))

    # 5. Einstieg - und warum an diesem Tag
    if signal.entry_date and signal.entry_price:
        eroeffnet = "Gekauft" if signal.signal_type == "BUY" else "Leerverkauft"
        hour = event.report_hour if event else None
        if signal.entry_date > signal.trigger_date:
            begruendung = (
                f"Gemeldet wurde {_hour_text(hour)}. Der Schlusskurs des Meldetages lag "
                "damit ganz oder teilweise vor der Nachricht – ihn zu nehmen hieße, den "
                "Kurssprung vorwegzunehmen. Deshalb der Folgetag."
                if hour in ("amc", "dmh")
                else
                "Der Meldezeitpunkt ist nicht bekannt. Um auf keinen Fall vor der Nachricht "
                "einzusteigen, wird grundsätzlich der Folgetag genommen."
            )
        else:
            begruendung = (
                "Gemeldet wurde vor Börsenbeginn, der Schlusskurs desselben Tages lag also "
                "bereits nach der Nachricht."
            )
        steps.append(Step(
            label="Einstieg",
            text=(
                f"{eroeffnet} wurde zum Schlusskurs vom {_tag(signal.entry_date)}: "
                f"{_num(signal.entry_price)}. {begruendung}"
            ),
        ))

    # 6. Ausstieg und Ergebnis
    if signal.exit_date and signal.exit_price and signal.return_pct is not None:
        vorzeichen = "gestiegen" if signal.exit_price > signal.entry_price else "gefallen"
        short_hinweis = (
            " Weil es ein Leerverkauf war, zählt ein fallender Kurs als Gewinn."
            if signal.signal_type == "SELL" else ""
        )
        steps.append(Step(
            label="Ausstieg",
            text=(
                f"Nach {signal.holding_period_days} Handelstagen, am {_tag(signal.exit_date)}, "
                f"stand der Kurs bei {_num(signal.exit_price)} – also "
                f"{vorzeichen}.{short_hinweis}"
            ),
            formula=(
                f"({_num(signal.exit_price)} − {_num(signal.entry_price)}) ÷ "
                f"{_num(signal.entry_price)} = {_pct(signal.return_pct, 2)}"
                if signal.signal_type == "BUY"
                else
                f"({_num(signal.entry_price)} − {_num(signal.exit_price)}) ÷ "
                f"{_num(signal.entry_price)} = {_pct(signal.return_pct, 2)}"
            ),
            tone="good" if signal.return_pct > 0 else "bad",
        ))

        steps.append(_benchmark_step(signal))

    return Explanation(
        headline=(
            f"{richtung}signal vom {_tag(signal.trigger_date)}"
            if signal.status == "OPEN"
            else f"{richtung}signal vom {_tag(signal.trigger_date)}, abgeschlossen"
        ),
        steps=steps,
        caveat=_pead_caveat(signal),
    )


def _pead_caveat(signal) -> str:
    brutto = ("Alle Renditen sind brutto – ohne Gebühren, Spread und Steuern. In der "
              "Realität bliebe weniger übrig.")
    if not getattr(signal, "retro", False):
        return brutto
    return ("Rückwirkend berechnet: Dieses Signal ist nicht am Tag der Meldung entstanden, "
            "sondern später aus heutigen Daten. Schätzungen und Gewinne werden nachträglich "
            "korrigiert, und die Titelauswahl kennt die Vergangenheit – damals hätte die "
            "Rechnung anders aussehen können. Es zählt deshalb nicht in die Live-Statistik. "
            + brutto)


def _benchmark_step(signal) -> Step:
    """Der eigentliche Lerngehalt: war das Signal besser als der Durchschnitt?"""
    benchmark = getattr(signal, "benchmark_return_pct", None)
    if benchmark is None:
        return Step(
            label="Einordnung",
            text=(
                "Für diesen Zeitraum liegt kein Vergleichswert der übrigen Titel vor. "
                "Ohne ihn lässt sich nicht sagen, ob das Ergebnis am Signal lag oder "
                "daran, dass sich der ganze Markt bewegt hat."
            ),
        )

    ueberrendite = signal.return_pct - benchmark
    if ueberrendite > 0:
        urteil = (
            f"Das Signal lag damit {_pct_plain(ueberrendite, 2)} besser als der Durchschnitt "
            "der übrigen beobachteten Titel im selben Zeitraum."
        )
        tone = "good"
    else:
        urteil = (
            f"Das Signal lag damit {_pct_plain(abs(ueberrendite), 2)} schlechter als der "
            "Durchschnitt der übrigen beobachteten Titel im selben Zeitraum – der Gewinn "
            "kam also eher vom Markt als vom Signal."
            if signal.return_pct > 0 else
            f"Der Durchschnitt der übrigen Titel lag bei {_pct(benchmark, 2)}; das Signal "
            f"blieb um {_pct_plain(abs(ueberrendite), 2)} dahinter."
        )
        tone = "bad"

    return Step(
        label="Einordnung",
        text=(
            f"Die übrigen beobachteten Titel machten im selben Zeitraum im Mittel "
            f"{_pct(benchmark, 2)}. {urteil} Diese Differenz ist die eigentliche Frage – "
            "eine Rendite für sich genommen sagt wenig."
        ),
        formula=(
            f"{_pct(signal.return_pct, 2)} {_minus(benchmark, 2)} "
            f"= {_pct(ueberrendite, 2)}"
        ),
        tone=tone,
    )


def explain_momentum(signal, rebalance, settings) -> Explanation:
    """Warum dieser Titel im Momentum-Korb liegt."""
    seite = "gekauft" if signal.direction == "LONG" else "leerverkauft"
    gruppe = "Spitzengruppe" if signal.direction == "LONG" else "Schlussgruppe"
    steps: list[Step] = []

    monate = round(settings.momentum.lookback_days / 21)
    if rebalance:
        steps.append(Step(
            label="Die Rangfolge",
            text=(
                f"Am {_tag(rebalance.rebalance_date)} wurden alle {rebalance.universe_size} "
                f"bewertbaren Titel nach ihrer Kursentwicklung der letzten {monate} Monate "
                f"sortiert – vom {_tag(rebalance.formation_start)} bis zum "
                f"{_tag(rebalance.formation_end)}."
            ),
        ))
        steps.append(Step(
            label="Warum der letzte Monat fehlt",
            text=(
                f"Die jüngsten {rebalance.skip_days} Handelstage bleiben absichtlich "
                "ausgespart. Auf sehr kurze Sicht neigen Kurse zur Gegenbewegung; würde "
                "man sie mitzählen, fräße das den Effekt auf."
            ),
        ))

    steps.append(Step(
        label="Die Platzierung",
        text=(
            f"Dieser Titel stieg im Formationsfenster um {_pct(signal.momentum_score)} und "
            f"landete damit auf Rang {signal.rank}"
            + (f" von {rebalance.universe_size}" if rebalance else "")
            + f". Die {gruppe} wird {seite}."
        ),
        formula=f"Rang {signal.rank}" + (f" von {rebalance.universe_size}" if rebalance else ""),
        tone="good" if signal.direction == "LONG" else "bad",
    ))

    steps.append(Step(
        label="Wichtig zu verstehen",
        text=(
            "Momentum ist eine <em>relative</em> Aussage: der Titel steht besser oder "
            "schlechter da als die anderen im Universum – nicht gut oder schlecht für sich. "
            "In einem fallenden Markt besteht die Spitzengruppe aus den kleinsten Verlusten."
        ),
    ))

    if signal.entry_date and signal.entry_price:
        steps.append(Step(
            label="Einstieg",
            text=(
                f"Eingestiegen am {_tag(signal.entry_date)} zum Schlusskurs "
                f"{_num(signal.entry_price)}, geplante Haltedauer "
                f"{signal.holding_period_days} Handelstage bis zur nächsten Umschichtung."
            ),
        ))

    if signal.exit_date and signal.exit_price and signal.return_pct is not None:
        steps.append(Step(
            label="Ausstieg",
            text=(
                f"Am {_tag(signal.exit_date)} stand der Kurs bei {_num(signal.exit_price)}."
                + (" Weil es ein Leerverkauf war, zählt ein fallender Kurs als Gewinn."
                   if signal.direction == "SHORT" else "")
            ),
            formula=(
                f"({_num(signal.exit_price)} − {_num(signal.entry_price)}) ÷ "
                f"{_num(signal.entry_price)} = {_pct(signal.return_pct, 2)}"
                if signal.direction == "LONG"
                else
                f"({_num(signal.entry_price)} − {_num(signal.exit_price)}) ÷ "
                f"{_num(signal.entry_price)} = {_pct(signal.return_pct, 2)}"
            ),
            tone="good" if signal.return_pct > 0 else "bad",
        ))
        steps.append(_benchmark_step(signal))

    return Explanation(
        headline=f"{signal.direction} aus der Umschichtung"
                 + (f" vom {_tag(rebalance.rebalance_date)}" if rebalance else ""),
        steps=steps,
        caveat=(
            "Bei einem kleinen Universum misst die Rangfolge überwiegend Branchenrotation, "
            "nicht den dokumentierten Momentum-Faktor."
        ),
    )
