/* Client-side behaviour: type-to-filter, and periodic status refresh.
 *
 * Refresh updates only the status dots and the header counters -- it does not
 * re-render the grid. Cards are stable elements so that hovering, an in-flight
 * middle-click, or a focused link is never destroyed under the user. When the
 * set of services actually changes (a container started or stopped), the DOM
 * cannot be patched safely, so the page reloads instead. */

(function () {
  "use strict";

  var POLL_MS = 30000;
  var filter = document.getElementById("filter");
  var grid = document.getElementById("grid");
  var updated = document.getElementById("updated");
  var statOnline = document.getElementById("stat-online");

  /* Filtering ---------------------------------------------------------- */

  function applyFilter() {
    var q = (filter.value || "").trim().toLowerCase();
    document.querySelectorAll(".cat").forEach(function (section) {
      var shown = 0;
      section.querySelectorAll(".card").forEach(function (card) {
        var hit = !q || card.dataset.name.indexOf(q) !== -1;
        card.hidden = !hit;
        if (hit) shown++;
      });
      section.hidden = shown === 0;

      // Keep the badge honest while filtering: it counts what is visible, and
      // returns to the section total once the query is cleared. The total is
      // remembered on first use because the rendered text is about to change.
      var badge = section.querySelector("h2 .count");
      if (badge) {
        if (!badge.dataset.total) badge.dataset.total = badge.textContent.trim();
        badge.textContent = q ? shown : badge.dataset.total;
      }
    });
  }

  if (filter) {
    filter.addEventListener("input", applyFilter);
    // "/" focuses the filter the way it does in most tools; Escape clears it.
    document.addEventListener("keydown", function (e) {
      if (e.key === "/" && document.activeElement !== filter) {
        e.preventDefault();
        filter.focus();
      } else if (e.key === "Escape" && document.activeElement === filter) {
        filter.value = "";
        applyFilter();
      }
    });
  }

  /* Status refresh ------------------------------------------------------ */

  function stamp(seconds) {
    if (!seconds) return "never scanned";
    var d = new Date(seconds * 1000);
    return "updated " + d.toLocaleTimeString();
  }

  function poll() {
    fetch("/api/apps", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var cards = document.querySelectorAll(".card");
        if (data.apps.length !== cards.length) {
          window.location.reload();
          return;
        }
        var up = 0;
        data.apps.forEach(function (item) {
          if (item.online) up++;
          var card = grid.querySelector('.card[data-key="' + CSS.escape(item.key) + '"]');
          if (!card) { window.location.reload(); return; }
          card.classList.toggle("down", item.online === false);
          card.classList.toggle("idle", item.online === null);
          card.classList.toggle("stopped", !item.running);
        });
        if (statOnline) {
          statOnline.innerHTML = "<strong>" + up + "</strong>/" + data.apps.length + " up";
        }
        updated.textContent = stamp(data.updated);
      })
      .catch(function () {
        updated.textContent = "refresh failed — server unreachable";
      });
  }

  poll();
  setInterval(poll, POLL_MS);
})();
