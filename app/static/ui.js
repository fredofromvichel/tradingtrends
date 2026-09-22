/* Kleine Bedienhilfen ohne Framework.
   - Tabellen mit [data-sortable] lassen sich per Kopfzeile sortieren.
     Sortiert wird nach data-sort der Zelle, nicht nach dem angezeigten Text:
     "+8,66 %" und "1 234,50" sind sonst nicht vergleichbar.
   - [data-filter] filtert die Zeilen der zugehörigen Tabelle.
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
        var rows = Array.prototype.slice.call(body.rows);
        rows.sort(function (a, b) {
          var x = sortValue(a.cells[index]), y = sortValue(b.cells[index]);
          if (x < y) return aufsteigend ? -1 : 1;
          if (x > y) return aufsteigend ? 1 : -1;
          return 0;
        });
        rows.forEach(function (row) { body.appendChild(row); });
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
      Array.prototype.forEach.call(table.tBodies[0].rows, function (row) {
        gesamt += 1;
        var treffer = !needle || row.textContent.toLowerCase().indexOf(needle) !== -1;
        row.hidden = !treffer;
        if (treffer) sichtbar += 1;
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
