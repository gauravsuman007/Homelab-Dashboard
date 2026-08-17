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

  function applyFilter() {
    var q = (filter.value || "").trim().toLowerCase();
    sections().forEach(function (section) {
      var shown = 0;
      section.querySelectorAll(".card").forEach(function (card) {
        var hit = !q || card.dataset.name.indexOf(q) !== -1;
        card.hidden = !hit;
        if (hit) shown++;
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
  }

  if (filter) {
    filter.addEventListener("input", applyFilter);
    document.addEventListener("keydown", function (e) {
      if (e.key === "/" && document.activeElement !== filter && !editing) {
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
    return "updated " + new Date(seconds * 1000).toLocaleTimeString();
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
        });
        if (statOnline) statOnline.innerHTML = "<strong>" + up + "</strong>/" + data.apps.length + " up";
        updated.textContent = stamp(data.updated);
      })
      .catch(function () { updated.textContent = "refresh failed — server unreachable"; });
  }

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
    ghost.classList.add("ghost");
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
