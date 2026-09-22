/* Kleine Bedienhilfen ohne Framework.
   - Tabellen mit [data-sortable] lassen sich per Kopfzeile sortieren.
     Sortiert wird nach data-sort der Zelle, nicht nach dem angezeigten Text:
     "+8,66 %" und "1 234,50" sind sonst nicht vergleichbar.
   - [data-filter] filtert die Zeilen der zugehörigen Tabelle.
   - [data-explain] klappt die Rechenweg-Zeile unter einer Tabellenzeile auf.
     Ohne JavaScript bleibt sie sichtbar, statt zu verschwinden.
   - j/k bzw. Pfeiltasten blättern zwischen Titeln. */
(function () {
  "use strict";

  function sortValue(cell) {
    if (cell === undefined) return "";
    var raw = cell.dataset.sort;
    if (raw === undefined) raw = cell.textContent.trim();
    var num = parseFloat(raw);
    return isNaN(num) || !/^[-+]?[\d.]+$/.test(raw) ? raw.toLowerCase() : num;
  }

  /* Eine Datenzeile und die ihr folgende Rechenweg-Zeile gehoeren zusammen.
     Wer nach Rendite sortiert, will den Rechenweg nicht woanders wiederfinden. */
  function gruppiere(body) {
    var bloecke = [];
    Array.prototype.forEach.call(body.rows, function (row) {
      if (row.classList.contains("explain-row")) {
        if (bloecke.length) bloecke[bloecke.length - 1].push(row);
      } else {
        bloecke.push([row]);
      }
    });
    return bloecke;
  }

  function explainRow(row) {
    var next = row.nextElementSibling;
    return next && next.classList.contains("explain-row") ? next : null;
  }

  function makeExplainable(btn) {
    var row = btn.parentNode;
    while (row && row.tagName !== "TR") row = row.parentNode;
    var ziel = row && explainRow(row);
    if (!ziel) return;
    ziel.hidden = true;
    btn.addEventListener("click", function () {
      var auf = ziel.hidden;
      ziel.hidden = !auf;
      btn.setAttribute("aria-expanded", auf ? "true" : "false");
    });
  }

  function makeSortable(table) {
    var headers = table.querySelectorAll("thead th");
    headers.forEach(function (th, index) {
      th.setAttribute("role", "columnheader");
      th.setAttribute("aria-sort", "none");
      th.tabIndex = 0;
      th.classList.add("sortable");

      function apply() {
        var aufsteigend = th.getAttribute("aria-sort") !== "ascending";
        headers.forEach(function (other) { other.setAttribute("aria-sort", "none"); });
        th.setAttribute("aria-sort", aufsteigend ? "ascending" : "descending");

        var body = table.tBodies[0];
        var bloecke = gruppiere(body);
        bloecke.sort(function (a, b) {
          var x = sortValue(a[0].cells[index]), y = sortValue(b[0].cells[index]);
          if (x < y) return aufsteigend ? -1 : 1;
          if (x > y) return aufsteigend ? 1 : -1;
          return 0;
        });
        bloecke.forEach(function (block) {
          block.forEach(function (row) { body.appendChild(row); });
        });
      }

      th.addEventListener("click", apply);
      th.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); apply(); }
      });
    });
  }

  function makeFilterable(input) {
    var table = document.querySelector(input.dataset.filter);
    if (!table) return;
    var status = document.querySelector(input.dataset.filterStatus || "");

    function apply() {
      var needle = input.value.trim().toLowerCase();
      var sichtbar = 0, gesamt = 0;
      gruppiere(table.tBodies[0]).forEach(function (block) {
        var row = block[0];
        gesamt += 1;
        var treffer = !needle || row.textContent.toLowerCase().indexOf(needle) !== -1;
        row.hidden = !treffer;
        if (treffer) {
          sichtbar += 1;
        } else if (block[1]) {
          // Ausgefilterte Zeile klappt zu, sonst steht ihr Rechenweg allein da.
          block[1].hidden = true;
          var btn = row.querySelector("[data-explain]");
          if (btn) btn.setAttribute("aria-expanded", "false");
        }
      });
      if (status) {
        status.textContent = needle
          ? sichtbar + " von " + gesamt + " Titeln"
          : gesamt + " Titel";
      }
    }
    input.addEventListener("input", apply);
    apply();
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("table[data-sortable]").forEach(makeSortable);
    document.querySelectorAll("[data-explain]").forEach(makeExplainable);
    document.querySelectorAll("[data-filter]").forEach(makeFilterable);

    // Blättern per Tastatur - nicht, während in ein Feld getippt wird.
    document.addEventListener("keydown", function (e) {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      var tag = (e.target.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select") return;

      var ziel = null;
      if (e.key === "j" || e.key === "ArrowRight") ziel = document.querySelector("[data-next]");
      if (e.key === "k" || e.key === "ArrowLeft") ziel = document.querySelector("[data-prev]");
      if (ziel) { e.preventDefault(); window.location.href = ziel.href; }
    });
  });
})();
