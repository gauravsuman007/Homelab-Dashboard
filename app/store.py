"""Persistence for customisations made in the UI.

Everything the edit mode changes -- renamed services, custom icons, categories
and which service sits in which -- lands in a single JSON file next to
``apps.yml``. JSON rather than YAML because this file is written by a machine:
round-tripping YAML would destroy the comments that make ``apps.yml`` worth
hand-editing, so the two are kept apart.

Precedence when the same field is set in more than one place:

    UI customisation  >  apps.yml  >  value derived from the Docker daemon

The UI wins because it is the most recent explicit instruction, and because a
change made by clicking should visibly stick. ``apps.yml`` stays the place for
things you want in version control.

Categories are stored as an ordered list, kept separately from the per-service
assignments, so a category can exist while empty -- otherwise a newly created
one would vanish before anything could be dragged into it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# The bucket that always exists and can never be renamed or removed: deleting a
# category has to put its services somewhere, and that somewhere must not itself
# be deletable or the invariant recurses.
UNCATEGORIZED = "Uncategorized"

SCHEMA_VERSION = 1
MAX_NAME_LENGTH = 60
# Mirrors the icon-slug rule in server.py: a value that ends up as a filename
# and as part of a URL is never trusted loose.
_ICON_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

# Appearance. Values are constrained to a known set rather than accepted as free
# text because they are interpolated straight into a style attribute -- a colour
# that can be anything is a stylesheet injection, so it has to look like a
# colour before it goes anywhere near the page.
_HEX_RE = re.compile(r"^#(?:[0-9a-f]{3}|[0-9a-f]{6})$")
THEMES = ("system", "dark", "light")
BACKGROUNDS = ("plain", "glow", "grid", "mesh", "image")
APPEARANCE_DEFAULTS = {
    "theme": "system",
    "accent": "",          # empty means the stylesheet's own accent
    "background": "plain",
    "background_url": "",  # a cached slug, not a remote URL
    "background_dim": 55,  # percent of scrim over an image, so text stays legible
    # Not a look, strictly, but it lives here for the same reason the rest
    # does: it is a shared choice about what the page shows, and the refresh
    # loop reads it to decide whether to ask the daemon for stats at all.
    "stats": True,
}
_FALSEY = {"false", "0", "no", "off", ""}


class ValidationError(ValueError):
    """A rejected edit. The message is shown to the user, so keep it readable."""


def clean_name(value: str, what: str = "name") -> str:
    """Normalise and validate a user-supplied name."""
    cleaned = " ".join((value or "").split())
    if not cleaned:
        raise ValidationError(f"The {what} cannot be empty.")
    if len(cleaned) > MAX_NAME_LENGTH:
        raise ValidationError(f"The {what} is too long (max {MAX_NAME_LENGTH} characters).")
    return cleaned


class Store:
    """Reads and writes the customisation file. Safe to share across threads."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data = self._load()

    # -- persistence ------------------------------------------------------

    def _load(self) -> dict:
        """Read the file, falling back to empty state on anything unreadable.

        A corrupt or hand-mangled file must not take the page down; it is
        renamed aside so the damage is recoverable and a fresh one is started.
        """
        if not self.path.is_file():
            return {"version": SCHEMA_VERSION, "categories": [], "apps": {}, "appearance": {}}
        try:
            data = json.loads(self.path.read_text())
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except (OSError, ValueError) as exc:
            log.warning("unreadable %s (%s); starting fresh", self.path, exc)
            try:
                self.path.rename(self.path.with_suffix(".json.corrupt"))
            except OSError:
                pass
            return {"version": SCHEMA_VERSION, "categories": [], "apps": {}, "appearance": {}}

        data.setdefault("version", SCHEMA_VERSION)
        data.setdefault("categories", [])
        data.setdefault("apps", {})
        data.setdefault("appearance", {})
        if not isinstance(data["appearance"], dict):
            data["appearance"] = {}
        if not isinstance(data["categories"], list):
            data["categories"] = []
        if not isinstance(data["apps"], dict):
            data["apps"] = {}
        return data

    def _save(self) -> None:
        """Write atomically, so an interrupted save cannot truncate the file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._data, indent=2, sort_keys=True) + "\n"
        handle = tempfile.NamedTemporaryFile(
            "w", dir=self.path.parent, prefix=".customisations-", suffix=".tmp", delete=False
        )
        try:
            with handle as out:
                out.write(payload)
                out.flush()
                os.fsync(out.fileno())
            # NamedTemporaryFile creates 0600, and this container runs as root:
            # left alone, the result would be a root-owned file the host user
            # cannot even read. This file is meant to be inspectable and
            # version-controllable next to apps.yml.
            os.chmod(handle.name, 0o644)
            os.replace(handle.name, self.path)
        except OSError as exc:
            Path(handle.name).unlink(missing_ok=True)
            raise ValidationError(
                "Could not save. Is ./config mounted read-only? See the README."
            ) from exc

    # -- reading ----------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def overrides(self) -> dict:
        """The stored customisations in the shape ``build_apps`` expects."""
        with self._lock:
            return {"apps": json.loads(json.dumps(self._data["apps"]))}

    def categories(self) -> list[str]:
        with self._lock:
            return list(self._data["categories"])

    def appearance(self) -> dict:
        """Stored look, with every unset field filled in from the defaults.

        Always complete, so callers never branch on a missing key and the
        template can interpolate the result directly.
        """
        with self._lock:
            stored = self._data.get("appearance") or {}
        return {**APPEARANCE_DEFAULTS, **{k: v for k, v in stored.items()
                                          if k in APPEARANCE_DEFAULTS}}

    def set_appearance(self, fields: dict) -> None:
        """Validate and store appearance fields; an empty value resets one.

        Same clear-to-reset rule the per-service edits use: sending "" for a
        field removes it, so "back to the default" needs no separate endpoint.
        """
        clean: dict = {}
        for key, raw in fields.items():
            if key not in APPEARANCE_DEFAULTS:
                raise ValidationError(f"Unknown appearance setting: {key}")
            value = raw if isinstance(raw, int) else str(raw or "").strip()

            if value == "" and key not in {"theme", "stats"}:
                clean[key] = ""
                continue
            if key == "theme":
                value = (value or "system").lower()
                if value not in THEMES:
                    raise ValidationError(f"Theme must be one of {', '.join(THEMES)}.")
            elif key == "accent":
                value = str(value).lower()
                if not _HEX_RE.match(value):
                    raise ValidationError("Accent must be a hex colour such as #5b8cff.")
            elif key == "background":
                value = str(value).lower()
                if value not in BACKGROUNDS:
                    raise ValidationError(f"Background must be one of {', '.join(BACKGROUNDS)}.")
            elif key == "background_url":
                # A cached slug, never a remote address: the server downloads
                # the image and hands back a name, so the page never fetches
                # from a third party and no URL from the form is ever rendered.
                if not _ICON_RE.match(str(value)):
                    raise ValidationError("Background image must be a cached name.")
            elif key == "stats":
                # Accepts the JSON boolean the page sends, and the string form
                # a hand-written curl is likely to use.
                value = value if isinstance(value, bool) else str(value).strip().lower() not in _FALSEY
            elif key == "background_dim":
                try:
                    value = max(0, min(90, int(value)))
                except (TypeError, ValueError):
                    raise ValidationError("Dim must be a number.") from None
            clean[key] = value

        with self._lock:
            current = dict(self._data.get("appearance") or {})
            for key, value in clean.items():
                if value == "" or value == APPEARANCE_DEFAULTS[key]:
                    current.pop(key, None)
                else:
                    current[key] = value
            self._data["appearance"] = current
            self._save()

    # -- per-service edits ------------------------------------------------

    def set_app_fields(self, key: str, fields: dict) -> None:
        """Set or clear customised fields for one container.

        A field passed as None or "" is *cleared*, which returns that aspect of
        the card to its derived value -- that is how "reset to automatic" works.
        An entry with nothing left in it is removed entirely rather than left as
        an empty object.
        """
        allowed = {"name", "icon", "category"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValidationError(f"Cannot set {', '.join(sorted(unknown))}.")

        with self._lock:
            entry = dict(self._data["apps"].get(key) or {})
            for field, value in fields.items():
                if value in (None, ""):
                    entry.pop(field, None)
                    continue
                if field == "icon":
                    if not _ICON_RE.match(value):
                        raise ValidationError(
                            "An icon name may use lowercase letters, digits, dots, "
                            "dashes and underscores only."
                        )
                    entry[field] = value
                elif field == "category":
                    entry[field] = clean_name(value, "category")
                else:
                    entry[field] = clean_name(value, "name")

            if entry:
                self._data["apps"][key] = entry
            else:
                self._data["apps"].pop(key, None)
            self._save()

    # -- categories -------------------------------------------------------

    def ensure_categories(self, names: list[str]) -> None:
        """Register categories that are in use but not yet listed.

        Categories derived from the built-in rules are not in the stored list
        until something makes them so. Registering them on sight gives every
        category a stable position and makes them editable, and it is idempotent
        -- nothing is written unless the list actually changed.
        """
        with self._lock:
            known = {c.casefold() for c in self._data["categories"]}
            added = [
                n for n in names
                if n and n != UNCATEGORIZED and n.casefold() not in known
            ]
            if not added:
                return
            self._data["categories"].extend(sorted(set(added)))
            self._save()

    def create_category(self, name: str) -> str:
        name = clean_name(name, "category")
        if name.casefold() == UNCATEGORIZED.casefold():
            raise ValidationError(f"{UNCATEGORIZED} already exists.")
        with self._lock:
            if any(c.casefold() == name.casefold() for c in self._data["categories"]):
                raise ValidationError(f"A category called {name!r} already exists.")
            self._data["categories"].append(name)
            self._save()
        return name

    def rename_category(self, old: str, new: str, members: list[str]) -> str:
        """Rename a category and move its members with it.

        ``members`` is every container currently displayed under ``old``,
        including those that landed there by derivation rather than by an
        explicit assignment. They each get an explicit assignment to the new
        name -- without that, a service whose *derived* category was the old
        name would spring back to it on the next scan.
        """
        new = clean_name(new, "category")
        if old == UNCATEGORIZED:
            raise ValidationError(f"{UNCATEGORIZED} cannot be renamed.")
        if new.casefold() == UNCATEGORIZED.casefold():
            raise ValidationError(f"{UNCATEGORIZED} is reserved.")

        with self._lock:
            existing = self._data["categories"]
            if any(c.casefold() == new.casefold() and c != old for c in existing):
                raise ValidationError(f"A category called {new!r} already exists.")
            self._data["categories"] = [new if c == old else c for c in existing]
            if not any(c == new for c in self._data["categories"]):
                self._data["categories"].append(new)
            for key in members:
                entry = dict(self._data["apps"].get(key) or {})
                entry["category"] = new
                self._data["apps"][key] = entry
            self._save()
        return new

    def delete_category(self, name: str, members: list[str]) -> None:
        """Remove a category; its members become Uncategorized.

        As with renaming, every member gets an explicit assignment, so a service
        that derived its way into this category does not reappear here on the
        next scan.
        """
        if name == UNCATEGORIZED:
            raise ValidationError(f"{UNCATEGORIZED} cannot be deleted.")
        with self._lock:
            self._data["categories"] = [c for c in self._data["categories"] if c != name]
            for key in members:
                entry = dict(self._data["apps"].get(key) or {})
                entry["category"] = UNCATEGORIZED
                self._data["apps"][key] = entry
            self._save()

    def reorder_categories(self, order: list[str]) -> None:
        """Store an explicit display order, keeping any category not mentioned."""
        with self._lock:
            known = self._data["categories"]
            seen = {c.casefold(): c for c in known}
            new_order = [seen[n.casefold()] for n in order if n.casefold() in seen]
            new_order += [c for c in known if c not in new_order]
            self._data["categories"] = new_order
            self._save()
