"""Host vitals, read with nothing the page did not already have.

The constraint here is the same one the rest of the project runs under: no extra
mounts, no agent on the host, no configuration. That rules out the usual sources
(``/host/proc``, a node-exporter sidecar, an SSH hop) and leaves two that are
already present in any container started from the shipped compose file:

* **``/proc``** -- ``stat``, ``meminfo`` and ``uptime`` are *not* namespaced by
  Docker, so a plain container reads the host's real numbers through its own
  ``/proc``. This is why the vitals need no privileges and no mount.
* **the Docker socket** -- for CPU count and total memory, which come from the
  daemon's own view of the machine and stay right even where ``/proc`` has been
  virtualised.

Two honest limits, both surfaced rather than papered over:

* Under **lxcfs** (LXC/Proxmox containers) or **Docker Desktop**, ``/proc`` is
  the VM's or the container's, not the metal's. The numbers are then true for
  the machine this container thinks it is on, which is the useful answer anyway.
* ``disk`` is the filesystem holding Docker's data, measured from ``/`` inside
  the container. On a normal install that is the host's main disk, which is what
  fills up and what people want warned about. It is not necessarily ``/`` on the
  host, so it is labelled for what it is.

Every field is independently optional. A source that cannot be read yields
``None`` for its own fields and never prevents the others from being reported --
a dashboard that vanishes because one number is unavailable is worse than one
showing three numbers out of four.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, asdict
from pathlib import Path

log = logging.getLogger(__name__)

PROC = Path("/proc")

# CPU utilisation is a rate, so it needs two samples. The previous one is kept
# here and differenced on the next call; the first call after start therefore
# reports None rather than a fabricated number.
_prev_cpu: tuple[int, int] | None = None


@dataclass
class Vitals:
    """A point-in-time reading of the host. Any field may be None."""

    cpu_percent: float | None = None
    load1: float | None = None
    ncpu: int | None = None
    mem_used: int | None = None
    mem_total: int | None = None
    disk_used: int | None = None
    disk_total: int | None = None
    uptime_seconds: float | None = None
    containers_running: int | None = None
    containers_total: int | None = None

    # Presentation is computed here rather than in the template and again in
    # JavaScript: the strip is rendered server-side for the first paint and
    # patched client-side by the poll, and those two must never disagree.

    @property
    def mem_percent(self) -> int:
        return _percent(self.mem_used, self.mem_total)

    @property
    def disk_percent(self) -> int:
        return _percent(self.disk_used, self.disk_total)

    @property
    def mem_text(self) -> str:
        return _pair(self.mem_used, self.mem_total)

    @property
    def disk_text(self) -> str:
        return _pair(self.disk_used, self.disk_total)

    @property
    def cpu_level(self) -> str:
        return _level(int(self.cpu_percent) if self.cpu_percent is not None else 0)

    @property
    def mem_level(self) -> str:
        return _level(self.mem_percent)

    @property
    def disk_level(self) -> str:
        return _level(self.disk_percent)

    @property
    def uptime_text(self) -> str:
        return _duration(self.uptime_seconds)

    def as_dict(self) -> dict:
        data = asdict(self)
        data.update(
            mem_percent=self.mem_percent,
            disk_percent=self.disk_percent,
            mem_text=self.mem_text,
            disk_text=self.disk_text,
            uptime_text=self.uptime_text,
            cpu_level=self.cpu_level,
            mem_level=self.mem_level,
            disk_level=self.disk_level,
        )
        return data


def _percent(used: int | None, total: int | None) -> int:
    """Whole-percent share, clamped, or 0 when either side is unknown."""
    if not used or not total:
        return 0
    return max(0, min(100, round(100 * used / total)))


def _level(percent: int) -> str:
    """Severity class for a meter: "" below 75%, "warn" to 90%, then "hot"."""
    if percent >= 90:
        return "hot"
    if percent >= 75:
        return "warn"
    return ""


def _pair(used: int | None, total: int | None) -> str:
    """"12.4 / 31.3 GB" -- one unit for both sides, so they compare at a glance."""
    if not total:
        return ""
    unit, scale = _unit(total)
    if used is None:
        return f"{total / scale:.0f} {unit}"
    return f"{used / scale:.1f} / {total / scale:.1f} {unit}"


def humanize_bytes(size: int | None) -> str:
    """"1.4 GB" for a single byte count -- distinct from _pair's used/total.

    Used for a container's own memory figure, where there is no "total" to
    show it against. "" for None, so a template can test truthiness
    directly instead of every caller checking for None first.
    """
    if size is None:
        return ""
    if size < 1024:
        return f"{size} B"
    unit, scale = _unit(size)
    return f"{size / scale:.1f} {unit}"


def _unit(size: int) -> tuple[str, float]:
    for unit, scale in (("TB", 1024 ** 4), ("GB", 1024 ** 3), ("MB", 1024 ** 2)):
        if size >= scale:
            return unit, float(scale)
    return "KB", 1024.0


def _duration(seconds: float | None) -> str:
    """Uptime at the coarsest useful resolution: "12d 4h", "3h 20m", "45m"."""
    if not seconds:
        return ""
    minutes = int(seconds // 60)
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def _read(name: str) -> str:
    """Contents of a /proc file, or "" when it cannot be read."""
    try:
        return (PROC / name).read_text()
    except OSError as exc:
        log.debug("cannot read /proc/%s: %s", name, exc)
        return ""


def _cpu_percent() -> float | None:
    """Host CPU utilisation since the previous call, or None on the first.

    Differences the aggregate jiffy counters in ``/proc/stat``. Because the
    caller invokes this once per refresh, the window is the refresh interval --
    a smoothed reading over ~30s rather than an instantaneous spike, which is
    the more useful number on a dashboard glanced at in passing.
    """
    global _prev_cpu

    line = next((l for l in _read("stat").splitlines() if l.startswith("cpu ")), "")
    fields = [int(v) for v in line.split()[1:] if v.isdigit()]
    if len(fields) < 4:
        return None

    total = sum(fields)
    # user, nice, system, idle, iowait, ... -- idle is index 3, iowait index 4.
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)

    previous, _prev_cpu = _prev_cpu, (total, idle)
    if previous is None:
        return None
    total_delta = total - previous[0]
    idle_delta = idle - previous[1]
    if total_delta <= 0:
        # Counters reset (host rebooted, or the file went unreadable between
        # samples). Report nothing rather than a nonsense percentage.
        return None
    return round(100.0 * (total_delta - idle_delta) / total_delta, 1)


def _memory() -> tuple[int | None, int | None]:
    """(used, total) host memory in bytes, from MemTotal and MemAvailable.

    MemAvailable rather than MemFree deliberately: free memory on a healthy
    server is near zero because the kernel uses it all for cache, so MemFree
    would report every box as permanently full.
    """
    values: dict[str, int] = {}
    for raw in _read("meminfo").splitlines():
        key, _, rest = raw.partition(":")
        if key in {"MemTotal", "MemAvailable"}:
            number = rest.strip().split()
            if number and number[0].isdigit():
                values[key] = int(number[0]) * 1024  # reported in kB
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if total is None or available is None:
        return None, total
    return max(total - available, 0), total


def _load1() -> float | None:
    """One-minute load average, or None."""
    parts = _read("loadavg").split()
    try:
        return float(parts[0])
    except (IndexError, ValueError):
        return None


def _uptime() -> float | None:
    """Host uptime in seconds, or None."""
    parts = _read("uptime").split()
    try:
        return float(parts[0])
    except (IndexError, ValueError):
        return None


def _disk() -> tuple[int | None, int | None]:
    """(used, total) bytes of the filesystem Docker's data lives on."""
    try:
        usage = shutil.disk_usage("/")
    except OSError as exc:
        log.debug("cannot stat /: %s", exc)
        return None, None
    return usage.used, usage.total


def read_vitals(client=None) -> Vitals:
    """Take one reading. Never raises; unreadable sources become None.

    ``client`` is an optional Docker client used only for the daemon's own view
    of CPU count, memory size and container counts. Omitting it still yields
    everything ``/proc`` can answer, which is what makes this testable with no
    daemon present.
    """
    mem_used, mem_total = _memory()
    disk_used, disk_total = _disk()
    vitals = Vitals(
        cpu_percent=_cpu_percent(),
        load1=_load1(),
        mem_used=mem_used,
        mem_total=mem_total,
        disk_used=disk_used,
        disk_total=disk_total,
        uptime_seconds=_uptime(),
    )

    if client is not None:
        try:
            info = client.info()
        except Exception as exc:  # any daemon hiccup; /proc data still stands
            log.debug("docker info unavailable: %s", exc)
        else:
            vitals.ncpu = info.get("NCPU") or None
            # The daemon knows the real machine even when /proc is virtualised.
            vitals.mem_total = info.get("MemTotal") or vitals.mem_total
            vitals.containers_total = info.get("Containers")
            vitals.containers_running = info.get("ContainersRunning")
    return vitals


# --------------------------------------------------------------------------
# Per-container memory and CPU
# --------------------------------------------------------------------------
#
# A container's own cgroup memory limit is usually unset, in which case Docker
# reports the *host's* total memory as the limit -- so "% of its own limit"
# would silently mean "% of the machine" for most containers anyway. Rather
# than present that as if it were a real cap, this reports the used bytes only;
# callers that want a percentage compare it against the host total from
# ``read_vitals`` themselves, which is the number that is actually true.
#
# The naive way to ask Docker for this, ``stats(stream=False)``, is not a
# snapshot: the daemon waits out a second sampling cycle (~1s) to compute a CPU
# delta, which measured at roughly 1.5s per container -- 40s for a modest
# fleet, sequentially. ``one_shot=True`` skips that wait; measured at ~3ms per
# container, which is why this is called sequentially per refresh rather than
# needing the thread-per-probe treatment the port checks use.
#
# The catch is that the wait is how the daemon gets its CPU delta, so one-shot
# stats arrive with ``precpu_stats`` zeroed and no percentage in them. What
# they do carry are the two cumulative counters the percentage is computed
# from, so the delta is taken here instead, between consecutive refreshes.
# That is strictly better for a dashboard than the daemon's own: the window is
# the refresh interval rather than one second, which is the same window the
# host CPU meter uses, so a spike does not show up in one figure and not the
# other.


@dataclass(frozen=True)
class Usage:
    """One container's raw usage counters, as of one moment.

    The CPU fields are cumulative totals, not rates -- meaningless alone, and
    turned into a percentage by ``cpu_percent_between`` against an earlier
    sample of the same container.
    """

    mem_used: int | None
    cpu_total: int | None
    cpu_system: int | None
    online_cpus: int


def container_usage(client, key: str) -> Usage | None:
    """One container's memory and CPU counters, or None.

    None means "not currently measurable" -- a stopped container (whose stats
    are an empty ``{}``, not an error) as much as an unreachable one. Both are
    ordinary, not a fault to log loudly about.
    """
    try:
        payload = client.api.stats(key, stream=False, one_shot=True)
    except Exception as exc:  # daemon hiccup, or the container vanished mid-scan
        log.debug("stats unavailable for %s: %s", key, exc)
        return None
    return _usage_from_stats(payload)


def _usage_from_stats(payload: dict) -> Usage | None:
    payload = payload or {}
    mem_used = _memory_from_stats(payload)
    cpu = (payload.get("cpu_stats") or {})
    total = (cpu.get("cpu_usage") or {}).get("total_usage")
    if mem_used is None and total is None:
        return None
    return Usage(
        mem_used=mem_used,
        cpu_total=None if total is None else int(total),
        cpu_system=cpu.get("system_cpu_usage"),
        # percpu_usage is empty under one-shot, so online_cpus is the only
        # count available here; 1 keeps the arithmetic sane if it is missing
        # too, at the cost of under-reporting rather than dividing by zero.
        online_cpus=int(cpu.get("online_cpus") or 1),
    )


def _memory_from_stats(payload: dict) -> int | None:
    mem = (payload or {}).get("memory_stats") or {}
    usage = mem.get("usage")
    if usage is None:  # a stopped container reports {} here, not an error
        return None
    # Page cache is reclaimable on demand and is not what "this service is
    # using 2GB" means to a person reading the card -- docker stats subtracts
    # it for the same reason. cgroup v2 names it inactive_file; v1 total_*.
    sub = mem.get("stats") or {}
    cache = sub.get("inactive_file", sub.get("total_inactive_file", sub.get("cache", 0)))
    return max(int(usage) - int(cache or 0), 0)


def cpu_percent_between(previous: Usage | None, current: Usage | None) -> float | None:
    """Percent of one CPU core used between two samples, or None.

    The scale is ``docker stats``' own -- 100% is one core saturated, so a
    container using two of four cores reads 200%, not 50%. Matching the tool
    people will check this against matters more than capping it at 100.

    None whenever no honest figure exists: the first sample of a container
    (nothing to difference against), a counter that went backwards or stood
    still (a restart, or a host clock the daemon re-based), or a missing
    counter. A blank is the truthful answer there; a zero would not be.
    """
    if previous is None or current is None:
        return None
    if None in (previous.cpu_total, current.cpu_total, previous.cpu_system, current.cpu_system):
        return None
    cpu_delta = current.cpu_total - previous.cpu_total
    system_delta = current.cpu_system - previous.cpu_system
    if system_delta <= 0 or cpu_delta < 0:
        return None
    return round(100.0 * cpu_delta / system_delta * current.online_cpus, 1)
