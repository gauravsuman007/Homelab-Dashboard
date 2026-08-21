/* Client behaviour: filtering, status refresh, and edit mode.
 *
 * Edit mode is a body class rather than a separate render: the server always
 * sends every category, including empty ones, so toggling needs no round trip
 * and cannot lose an unsaved drag.
 *
 * Every edit is written server-side before the UI accepts it, and the response
 * carries the authoritative values back. On failure the change is reverted and
 * the reason shown, so the page never displays a state the server did not save. */

(function () {
  "use strict";

  var POLL_MS = 30000;
  var body = document.body;
  var grid = document.getElementById("grid");
  var filter = document.getElementById("filter");
  var updated = document.getElementById("updated");
  var statOnline = document.getElementById("stat-online");
  var editToggle = document.getElementById("edit-toggle");
  var editHint = document.getElementById("edit-hint");
  var addCat = document.getElementById("cat-add");
  var UNCATEGORIZED = body.dataset.uncategorized || "Uncategorized";
  var editing = false;

  /* Helpers ------------------------------------------------------------- */

  function post(url, payload) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {})
    }).then(function (r) {
      return r.json().then(function (data) {
        if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
        return data;
      });
    });
  }

  function toast(message) {
    var el = document.createElement("div");
    el.className = "toast";
    el.textContent = message;
    document.body.appendChild(el);
    setTimeout(function () { el.remove(); }, 4000);
  }

  function sections() {
    return Array.prototype.slice.call(grid.querySelectorAll(".cat"));
  }

  function sectionFor(name) {
    return sections().filter(function (s) { return s.dataset.cat === name; })[0];
  }

  /* Filtering ----------------------------------------------------------- */

  /* Live search ---------------------------------------------------------
   *
   * Subsequence matching, not substring: "abs" finds Audiobookshelf and "jf"
   * finds Jellyfin, which is how people actually type when they already know
   * what they are looking for. Ranking puts a prefix hit above a word-boundary
   * hit above a scattered one, so the top result is the obvious one and Enter
   * can be trusted to open it.
   *
   * Everything runs against the DOM already on the page -- there is no search
   * endpoint and no round trip, so results keep up with a held-down key. */

  var searchCount = document.getElementById("search-count");
  var selected = null;

  function score(haystack, needle) {
    // -1 for no match; lower is better.
    if (!needle) return 0;

    // A literal substring always beats a scattered match, however tight the
    // scatter -- typing "arr" should not rank Radarr below something whose
    // a, r and r happen to sit close together. Earliest occurrence wins.
    var direct = haystack.indexOf(needle);
    if (direct >= 0) return direct;

    var i = 0, j = 0, gaps = 0, first = -1, boundary = false;
    while (i < haystack.length && j < needle.length) {
      if (haystack.charAt(i) === needle.charAt(j)) {
        if (first < 0) {
          first = i;
          boundary = i === 0 || " -_./(".indexOf(haystack.charAt(i - 1)) !== -1;
        }
        j++;
      } else if (j > 0) {
        gaps++;
      }
      i++;
    }
    if (j < needle.length) return -1;
    // Where the match starts matters far more than how tightly it is packed:
    // "abs" should find Audiobookshelf, whose letters are spread across the
    // word, ahead of a name where a-b-s happen to fall adjacent mid-string.
    // The 1000 floor keeps every subsequence hit below every substring hit.
    return 1000 + first * 6 + gaps * 2 + (boundary ? 0 : 8);
  }

  function visibleCards() {
    return Array.prototype.slice
      .call(grid.querySelectorAll(".card"))
      .filter(function (c) { return !c.hidden; });
  }

  function select(card) {
    if (selected) selected.classList.remove("selected");
    selected = card || null;
    if (selected) {
      selected.classList.add("selected");
      selected.scrollIntoView({ block: "nearest" });
    }
  }

  function applyFilter() {
    var q = (filter.value || "").trim().toLowerCase();
    var hits = [];

    sections().forEach(function (section) {
      var shown = 0;
      section.querySelectorAll(".card").forEach(function (card) {
        var rank = score(card.dataset.name, q);
        var hit = rank >= 0;
        card.hidden = !hit;
        if (hit) {
          shown++;
          hits.push({ card: card, rank: rank });
        }
      });
      // In edit mode every category stays visible: an empty one is still a
      // drop target, and hiding it mid-drag would be hostile.
      section.hidden = shown === 0 && !editing;
      section.classList.toggle("empty", section.querySelectorAll(".card").length === 0);

      var badge = section.querySelector("h2 .count");
      if (badge) {
        if (!badge.dataset.total) badge.dataset.total = badge.textContent.trim();
        badge.textContent = q ? shown : section.querySelectorAll(".card").length;
      }
    });

    if (searchCount) {
      searchCount.textContent = q ? (hits.length + (hits.length === 1 ? " match" : " matches")) : "";
      searchCount.classList.toggle("none", Boolean(q) && hits.length === 0);
    }
    if (filter) filter.setAttribute("aria-expanded", String(Boolean(q) && hits.length > 0));

    // Pre-select the best match so Enter is immediately useful.
    hits.sort(function (a, b) { return a.rank - b.rank; });
    select(q && hits.length ? hits[0].card : null);
  }

  function openSelected(newTab) {
    var card = selected || visibleCards()[0];
    if (!card) return;
    var link = card.querySelector("a.hit");
    if (!link) return;
    if (newTab) window.open(link.href, "_blank", "noreferrer");
    else window.location.href = link.href;
  }

  function step(delta) {
    var cards = visibleCards();
    if (!cards.length) return;
    var index = cards.indexOf(selected);
    index = index < 0 ? (delta > 0 ? 0 : cards.length - 1)
                      : (index + delta + cards.length) % cards.length;
    select(cards[index]);
  }

  if (filter) {
    filter.addEventListener("input", applyFilter);

    filter.addEventListener("keydown", function (e) {
      if (e.key === "ArrowDown") { e.preventDefault(); step(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); step(-1); }
      else if (e.key === "Enter") {
        e.preventDefault();
        // Meta/Ctrl matches the browser convention for "open in a new tab".
        openSelected(e.metaKey || e.ctrlKey);
      }
    });

    document.addEventListener("keydown", function (e) {
      var typing = /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName)
        || document.activeElement.isContentEditable;
      if ((e.key === "/" || (e.key === "k" && (e.metaKey || e.ctrlKey))) && !typing && !editing) {
        e.preventDefault();
        filter.focus();
        filter.select();
      } else if (e.key === "Escape" && document.activeElement === filter) {
        filter.value = "";
        applyFilter();
        filter.blur();
      }
    });
  }

  /* Status refresh ------------------------------------------------------ */

  // The server computes every displayed string, so the polled update and the
  // server-rendered first paint cannot drift apart in formatting.
  function paintVitals(v) {
    var strip = document.getElementById("vitals");
    if (!strip || !v) return;
    [["cpu", v.cpu_percent, Math.round(v.cpu_percent || 0) + "%"],
     ["mem", v.mem_percent, v.mem_text],
     ["disk", v.disk_percent, v.disk_text],
     ["up", null, v.uptime_text]].forEach(function (row) {
      var el = strip.querySelector('[data-vital="' + row[0] + '"]');
      if (!el) return;
      var known = row[2] !== "" && row[2] !== null && row[2] !== undefined
        && !(row[0] === "cpu" && v.cpu_percent === null);
      el.hidden = !known;
      if (!known) return;
      var bar = el.querySelector(".meter i");
      if (bar && row[1] !== null) bar.style.width = row[1] + "%";
      el.querySelector(".vital-value").textContent = row[2];
    });
  }

  function stamp(seconds) {
    if (!seconds) return "never scanned";
    return "updated " + new Date(seconds * 1000).toLocaleTimeString();
  }

  // Mirrors stats.humanize_bytes exactly (same thresholds, same one decimal
  // place) so a number patched in by polling is never visibly different from
  // the one the server rendered on first paint.
  function formatBytes(n) {
    if (n === null || n === undefined) return "";
    if (n < 1024) return n + " B";
    var units = ["KB", "MB", "GB", "TB"], scale = 1024, unit = units[0];
    for (var i = 0; i < units.length; i++) {
      if (n < Math.pow(1024, i + 2)) { unit = units[i]; scale = Math.pow(1024, i + 1); break; }
      unit = units[i];
      scale = Math.pow(1024, i + 1);
    }
    return (n / scale).toFixed(1) + " " + unit;
  }

  // Per-card memory and storage change slowly -- a container's RAM drifts a
  // little every refresh, its disk usage barely at all -- so this only
  // updates the number already on the page. It does not add the line to a
  // card that does not have one yet (e.g. a container that just started):
  // that appears on the next full page load, same as a new card would.
  function paintUsage(card, item) {
    var mem = card.querySelector('[data-usage="mem"]');
    if (mem && item.mem_used !== null && item.mem_used !== undefined) {
      mem.querySelector(".n").textContent = formatBytes(item.mem_used);
      mem.title = formatBytes(item.mem_used) + " resident" +
        (item.mem_host_percent ? ", " + item.mem_host_percent + "% of host RAM" : "");
    }
    var store = card.querySelector('[data-usage="storage"]');
    if (store && item.storage) {
      var total = item.storage.container + item.storage.volumes + (item.storage.binds || 0);
      store.querySelector(".n").textContent = formatBytes(total);
    }
  }

  function poll() {
    // Never reload or repaint underneath someone who is editing.
    if (editing) return;
    fetch("/api/apps", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var cards = document.querySelectorAll(".card");
        if (data.apps.length !== cards.length) { window.location.reload(); return; }
        var up = 0;
        data.apps.forEach(function (item) {
          if (item.online) up++;
          var card = grid.querySelector('.card[data-key="' + CSS.escape(item.key) + '"]');
          if (!card) { window.location.reload(); return; }
          card.classList.toggle("down", item.online === false);
          card.classList.toggle("idle", item.online === null);
          card.classList.toggle("stopped", !item.running);
          paintUsage(card, item);
        });
        if (statOnline) statOnline.innerHTML = "<strong>" + up + "</strong>/" + data.apps.length + " up";
        paintVitals(data.vitals);
        updated.textContent = stamp(data.updated);
      })
      .catch(function () { updated.textContent = "refresh failed — server unreachable"; });
  }

  /* Appearance ----------------------------------------------------------
   *
   * Theme has three states, and the third one matters: "system" means *no*
   * data-theme attribute, which is what lets the stylesheet's media query
   * follow the device. Storing "dark" as the default would freeze every
   * visitor's palette to whatever the last person picked.
   *
   * Accent and background are shared, like renames are -- one dashboard for
   * one household. A single device can still pin its own palette with
   * ?theme=, which the server resolves ahead of the stored value. */

  var THEMES = ["system", "dark", "light"];
  var themeToggle = document.getElementById("theme-toggle");
  var appearanceDialog = document.getElementById("appearance");
  var look = { theme: "system", accent: "", background: "plain", background_dim: 55 };

  function applyTheme(name) {
    if (name === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", name);
    if (themeToggle) {
      themeToggle.title = name === "system"
        ? "Theme: follows this device" : "Theme: " + name;
    }
  }

  function saveLook(fields, onError) {
    return post("/api/appearance", fields)
      .then(function (data) { look = data.appearance; return look; })
      .catch(function (err) {
        if (onError) onError(err);
        else toast(err.message);
        throw err;
      });
  }

  if (themeToggle) {
    themeToggle.addEventListener("click", function () {
      var next = THEMES[(THEMES.indexOf(look.theme) + 1) % THEMES.length];
      var previous = look.theme;
      // Paint first: a theme flip should feel instant, and the server round
      // trip only decides whether it survives a reload.
      look.theme = next;
      applyTheme(next);
      saveLook({ theme: next }, function (err) {
        look.theme = previous;
        applyTheme(previous);
        toast(body.dataset.canEdit ? err.message : "Editing is disabled, so the theme is not saved.");
      });
    });
  }

  function markChoice(container, value) {
    container.querySelectorAll("button").forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.dataset.value === value));
    });
  }

  function paintAppearanceForm() {
    markChoice(document.getElementById("a-theme"), look.theme);
    markChoice(document.getElementById("a-accent"), look.accent || "");
    markChoice(document.getElementById("a-background"), look.background);
    var dim = document.getElementById("a-dim");
    dim.value = look.background_dim;
    document.getElementById("a-dim-value").textContent = look.background_dim + "%";
  }

  function wireAppearance() {
    if (!appearanceDialog) return;
    var error = document.getElementById("appearance-error");

    function attempt(fields) {
      error.hidden = true;
      // The look is applied by the stylesheet on the next render, so a reload
      // is the honest way to show the result rather than duplicating every
      // rule in JavaScript.
      saveLook(fields, function (err) {
        error.textContent = err.message;
        error.hidden = false;
      }).then(function () { window.location.reload(); });
    }

    document.getElementById("a-theme").addEventListener("click", function (e) {
      var b = e.target.closest("button[data-value]");
      if (b) attempt({ theme: b.dataset.value });
    });
    document.getElementById("a-accent").addEventListener("click", function (e) {
      var b = e.target.closest("button[data-value]");
      if (b) attempt({ accent: b.dataset.value });
    });
    document.getElementById("a-background").addEventListener("click", function (e) {
      var b = e.target.closest("button[data-value]");
      if (b) attempt({ background: b.dataset.value });
    });
    document.getElementById("a-accent-custom").addEventListener("change", function (e) {
      attempt({ accent: e.target.value });
    });
    document.getElementById("a-bg-url").addEventListener("change", function (e) {
      if (e.target.value.trim()) attempt({ background_image_url: e.target.value.trim() });
    });

    var dim = document.getElementById("a-dim");
    dim.addEventListener("input", function () {
      document.getElementById("a-dim-value").textContent = dim.value + "%";
    });
    dim.addEventListener("change", function () { attempt({ background_dim: Number(dim.value) }); });

    document.getElementById("appearance-reset").addEventListener("click", function () {
      attempt({ accent: "", background: "plain", theme: "system", background_dim: 55 });
    });
    document.getElementById("appearance-close").addEventListener("click", function () {
      appearanceDialog.close();
    });

    var open = document.getElementById("appearance-open");
    if (open) {
      open.addEventListener("click", function () {
        paintAppearanceForm();
        appearanceDialog.showModal();
      });
    }
  }

  // The server rendered the current look already; this only syncs the JS copy
  // so the toggle knows where in the cycle it is.
  fetch("/api/state", { cache: "no-store" })
    .then(function (r) { return r.json(); })
    .then(function (data) { if (data.appearance) look = data.appearance; })
    .catch(function () {});
  wireAppearance();

  /* Edit mode ----------------------------------------------------------- */

  function setEditing(on) {
    editing = on;
    body.classList.toggle("editing", on);
    editToggle.setAttribute("aria-pressed", String(on));
    editToggle.querySelector(".label").textContent = on ? "Done" : "Edit";
    if (editHint) editHint.hidden = !on;
    if (addCat) addCat.hidden = !on;
    document.querySelectorAll(".card").forEach(function (c) { c.draggable = on; });
    document.querySelectorAll(".cat-name").forEach(function (n) {
      n.contentEditable = on ? "true" : "false";
    });
    applyFilter();
    if (!on) poll();
  }

  if (editToggle) {
    editToggle.addEventListener("click", function () { setEditing(!editing); });
  }

  /* Drag and drop between categories */

  var dragged = null;

  grid.addEventListener("dragstart", function (e) {
    var card = e.target.closest && e.target.closest(".card");
    if (!editing || !card) return;
    dragged = card;
    card.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    // Firefox refuses to start a drag without payload.
    e.dataTransfer.setData("text/plain", card.dataset.key);
  });

  grid.addEventListener("dragend", function () {
    if (dragged) dragged.classList.remove("dragging");
    dragged = null;
    document.querySelectorAll(".cards.over").forEach(function (z) { z.classList.remove("over"); });
  });

  grid.addEventListener("dragover", function (e) {
    if (!editing || !dragged) return;
    var zone = e.target.closest && e.target.closest(".cards");
    if (!zone) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    zone.classList.add("over");
  });

  grid.addEventListener("dragleave", function (e) {
    var zone = e.target.closest && e.target.closest(".cards");
    if (zone && !zone.contains(e.relatedTarget)) zone.classList.remove("over");
  });

  grid.addEventListener("drop", function (e) {
    if (!editing || !dragged) return;
    var zone = e.target.closest && e.target.closest(".cards");
    if (!zone) return;
    e.preventDefault();
    zone.classList.remove("over");
    moveCard(dragged, zone.dataset.drop);
  });

  /* Pointer-based dragging.
   *
   * HTML5 drag and drop does not exist on touch devices, so the grip handles
   * use pointer events instead -- one code path for mouse, pen and touch. It is
   * bound to the grips rather than the whole card so that a normal swipe still
   * scrolls the page; the grip itself sets touch-action:none so a drag starting
   * there does not scroll. Dragging a card's grip re-files it; dragging a
   * category's grip reorders the sections. */

  function makeGhost(source, event) {
    var rect = source.getBoundingClientRect();
    var ghost = source.cloneNode(true);
    ghost.classList.add("drag-ghost");
    ghost.style.width = rect.width + "px";
    ghost.style.left = rect.left + "px";
    ghost.style.top = rect.top + "px";
    ghost.dataset.dx = event.clientX - rect.left;
    ghost.dataset.dy = event.clientY - rect.top;
    document.body.appendChild(ghost);
    return ghost;
  }

  function positionGhost(ghost, event) {
    ghost.style.left = (event.clientX - Number(ghost.dataset.dx)) + "px";
    ghost.style.top = (event.clientY - Number(ghost.dataset.dy)) + "px";
  }

  // Keep the pointer able to reach the edges of a long page while dragging.
  function edgeScroll(y) {
    if (y < 90) window.scrollBy(0, -14);
    else if (y > window.innerHeight - 90) window.scrollBy(0, 14);
  }

  function beginPointerDrag(event, handle, spec) {
    if (!editing || event.button > 0) return;
    // Suppresses the native HTML5 drag and any text selection, so the two
    // mechanisms can never both run for one gesture.
    event.preventDefault();

    var source = spec.source(handle);
    var ghost = makeGhost(source, event);
    source.classList.add("dragging");
    handle.setPointerCapture(event.pointerId);

    function move(e) {
      positionGhost(ghost, e);
      edgeScroll(e.clientY);
      ghost.style.visibility = "hidden";          // look *through* the ghost
      var under = document.elementFromPoint(e.clientX, e.clientY);
      ghost.style.visibility = "";
      spec.over(source, under, e);
    }

    function end(e) {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", end);
      handle.removeEventListener("pointercancel", end);
      ghost.remove();
      source.classList.remove("dragging");
      spec.drop(source, e);
    }

    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", end);
    handle.addEventListener("pointercancel", end);
  }

  grid.addEventListener("pointerdown", function (e) {
    var cardGrip = e.target.closest && e.target.closest(".card .grip");
    if (cardGrip) {
      beginPointerDrag(e, cardGrip, {
        source: function (h) { return h.closest(".card"); },
        over: function (source, under) {
          document.querySelectorAll(".cards.over").forEach(function (z) {
            z.classList.remove("over");
          });
          var zone = under && under.closest && under.closest(".cards");
          if (zone) zone.classList.add("over");
        },
        drop: function (source, e) {
          var under = document.elementFromPoint(e.clientX, e.clientY);
          var zone = under && under.closest && under.closest(".cards");
          document.querySelectorAll(".cards.over").forEach(function (z) {
            z.classList.remove("over");
          });
          if (zone) moveCard(source, zone.dataset.drop);
        }
      });
      return;
    }

    var catGrip = e.target.closest && e.target.closest(".cat-grip");
    if (catGrip) {
      beginPointerDrag(e, catGrip, {
        source: function (h) { return h.closest(".cat"); },
        // Sections are reordered live, so the page shows the outcome before the
        // gesture ends. Uncategorized is skipped: it is pinned last.
        over: function (source, under, e) {
          var target = under && under.closest && under.closest(".cat");
          if (!target || target === source || target.dataset.fixed) return;
          var box = target.getBoundingClientRect();
          var after = e.clientY > box.top + box.height / 2;
          target.parentNode.insertBefore(source, after ? target.nextSibling : target);
        },
        drop: function () { saveCategoryOrder(); }
      });
    }
  });

  function saveCategoryOrder() {
    var order = sections()
      .filter(function (s) { return !s.dataset.fixed; })
      .map(function (s) { return s.dataset.cat; });
    post("/api/categories", { action: "reorder", order: order })
      .catch(function (err) {
        toast("Could not save order: " + err.message);
        window.location.reload();
      });
  }

  function moveCard(card, category) {
    var from = card.closest(".cat").dataset.cat;
    if (from === category) return;
    var zone = sectionFor(category).querySelector(".cards");
    var previous = card.nextSibling;
    var origin = card.parentNode;
    zone.appendChild(card);          // optimistic
    applyFilter();

    post("/api/app/" + encodeURIComponent(card.dataset.key), { category: category })
      .catch(function (err) {
        origin.insertBefore(card, previous);  // put it back exactly where it was
        applyFilter();
        toast("Could not move: " + err.message);
      });
  }

  /* Category create / rename / delete */

  if (addCat) {
    addCat.addEventListener("click", function () {
      var name = window.prompt("New category name");
      if (!name) return;
      post("/api/categories", { action: "create", name: name })
        .then(function () { window.location.reload(); })
        .catch(function (err) { toast(err.message); });
    });
  }

  grid.addEventListener("click", function (e) {
    var del = e.target.closest && e.target.closest(".cat-del");
    if (!editing || !del) return;
    var section = del.closest(".cat");
    var name = section.dataset.cat;
    var count = section.querySelectorAll(".card").length;
    var message = count
      ? 'Delete "' + name + '"? Its ' + count + ' service(s) move to ' + UNCATEGORIZED + "."
      : 'Delete "' + name + '"?';
    if (!window.confirm(message)) return;

    post("/api/categories", { action: "delete", name: name })
      .then(function () { window.location.reload(); })
      .catch(function (err) { toast(err.message); });
  });

  // Category renaming, committed on blur or Enter.
  grid.addEventListener("keydown", function (e) {
    if (e.target.classList.contains("cat-name") && e.key === "Enter") {
      e.preventDefault();
      e.target.blur();
    } else if (e.target.classList.contains("cat-name") && e.key === "Escape") {
      e.target.textContent = e.target.dataset.value;
      e.target.blur();
    }
  });

  grid.addEventListener("blur", function (e) {
    var label = e.target;
    if (!label.classList || !label.classList.contains("cat-name")) return;
    var old = label.dataset.value;
    var next = (label.textContent || "").trim();
    if (!next || next === old) { label.textContent = old; return; }

    post("/api/categories", { action: "rename", name: old, new_name: next })
      .then(function () { window.location.reload(); })
      .catch(function (err) {
        label.textContent = old;
        toast(err.message);
      });
  }, true);

  /* Card editor --------------------------------------------------------- */

  var dialog = document.getElementById("editor");
  var fName = document.getElementById("f-name");
  var fIcon = document.getElementById("f-icon");
  var fIconUrl = document.getElementById("f-icon-url");
  var fCat = document.getElementById("f-cat");
  var preview = document.getElementById("icon-preview");
  var errorBox = document.getElementById("editor-error");
  var current = null;

  function openEditor(card) {
    current = card;
    var key = card.dataset.key;
    fetch("/api/state").then(function (r) { return r.json(); }).then(function (state) {
      var item = state.apps.filter(function (a) { return a.key === key; })[0];
      if (!item) return;

      document.getElementById("editor-key").textContent = key;
      document.getElementById("d-name").textContent = item.derived.name;
      var custom = (state.customisations.apps || {})[key] || {};
      fName.value = custom.name || "";
      fName.placeholder = item.derived.name;
      fIcon.value = custom.icon || "";
      fIcon.placeholder = item.derived.icon;
      fIconUrl.value = "";
      preview.src = "/icon/" + encodeURIComponent(item.icon) + "?app=" + encodeURIComponent(key);

      fCat.innerHTML = "";
      state.categories.forEach(function (c) {
        var opt = document.createElement("option");
        opt.value = c;
        opt.textContent = c;
        if (c === item.category) opt.selected = true;
        fCat.appendChild(opt);
      });

      errorBox.hidden = true;
      dialog.showModal();
      fName.focus();
    });
  }

  grid.addEventListener("click", function (e) {
    if (!editing) return;
    var card = e.target.closest && e.target.closest(".card");
    if (!card || e.target.closest(".cat-del")) return;
    e.preventDefault();   // in edit mode a card opens the editor, not the link
    openEditor(card);
  });

  if (fIcon) {
    fIcon.addEventListener("input", function () {
      var slug = fIcon.value.trim() || fIcon.placeholder;
      if (slug) preview.src = "/icon/" + encodeURIComponent(slug);
    });
  }

  function closeEditor() { dialog.close(); current = null; }

  document.getElementById("editor-cancel").addEventListener("click", closeEditor);

  document.getElementById("editor-save").addEventListener("click", function () {
    if (!current) return;
    var payload = {
      name: fName.value.trim(),
      icon: fIcon.value.trim(),
      category: fCat.value
    };
    if (fIconUrl.value.trim()) payload.icon_url = fIconUrl.value.trim();

    post("/api/app/" + encodeURIComponent(current.dataset.key), payload)
      .then(function () { window.location.reload(); })
      .catch(function (err) {
        errorBox.textContent = err.message;
        errorBox.hidden = false;
      });
  });

  document.getElementById("editor-reset").addEventListener("click", function () {
    if (!current) return;
    post("/api/app/" + encodeURIComponent(current.dataset.key),
         { name: "", icon: "", category: "" })
      .then(function () { window.location.reload(); })
      .catch(function (err) {
        errorBox.textContent = err.message;
        errorBox.hidden = false;
      });
  });

  /* Start --------------------------------------------------------------- */

  applyFilter();
  poll();
  setInterval(poll, POLL_MS);
})();
