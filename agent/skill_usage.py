"""Lightweight per-skill usage tracking for relevance ranking.

Records ``load_count`` and ``last_loaded_at`` for each skill the agent
loads via ``skill_view``.  Persisted as a single JSON file at
``<skills_dir>/.usage.json`` so it survives across sessions and profiles
(profile-aware via ``get_skills_dir()``).

The data is consumed by ``build_skills_system_prompt`` to sort the
in-prompt skill index — frequently-used and recently-used skills float
to the top of each category, replacing the previous strict alphabetical
order.

Design notes:

  - **Best-effort writes** — never raise into the caller.  The tracker
    is a UX nicety, not a correctness boundary; a corrupted JSON or a
    missing skills dir must never break ``skill_view``.
  - **Process-local cache + atomic write** — reads are cached for the
    process lifetime (the dict is loaded once, mutated in-memory, and
    flushed on every record).  Atomic ``os.replace`` avoids partial
    writes when the process exits mid-flush.
  - **Bounded size** — we only keep the last ``MAX_TRACKED_SKILLS``
    entries, evicting by oldest ``last_loaded_at`` when the cap is hit.
    Prevents the file from growing unbounded if the agent enumerates
    every skill.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from hermes_constants import get_skills_dir

logger = logging.getLogger(__name__)


USAGE_FILENAME = ".usage.json"
MAX_TRACKED_SKILLS = 500

_LOCK = threading.RLock()
_CACHE: Optional[Dict[str, Dict[str, float]]] = None
_CACHE_PATH: Optional[Path] = None


def _usage_path() -> Path:
    return get_skills_dir() / USAGE_FILENAME


def _load_locked() -> Dict[str, Dict[str, float]]:
    global _CACHE, _CACHE_PATH
    path = _usage_path()
    if _CACHE is not None and _CACHE_PATH == path:
        return _CACHE
    data: Dict[str, Dict[str, float]] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                # Validate shape: drop any entry that doesn't look right
                for name, entry in raw.items():
                    if not isinstance(name, str) or not isinstance(entry, dict):
                        continue
                    try:
                        load_count = int(entry.get("load_count", 0))
                        last_loaded_at = float(entry.get("last_loaded_at", 0))
                    except (TypeError, ValueError):
                        continue
                    data[name] = {
                        "load_count": load_count,
                        "last_loaded_at": last_loaded_at,
                    }
        except (OSError, json.JSONDecodeError) as e:
            logger.debug("skill_usage: unreadable usage file %s: %s", path, e)
    _CACHE = data
    _CACHE_PATH = path
    return data


def _save_locked(data: Dict[str, Dict[str, float]]) -> None:
    path = _usage_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=".usage_", suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, str(path))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as e:
        logger.debug("skill_usage: failed to persist %s: %s", path, e)


def _evict_if_over_cap_locked(data: Dict[str, Dict[str, float]]) -> None:
    if len(data) <= MAX_TRACKED_SKILLS:
        return
    by_recency = sorted(data.items(), key=lambda kv: kv[1].get("last_loaded_at", 0))
    drop_count = len(data) - MAX_TRACKED_SKILLS
    for name, _ in by_recency[:drop_count]:
        data.pop(name, None)


def record_skill_load(name: str) -> None:
    """Bump the load counter and timestamp for ``name``.

    Called from ``skill_view`` whenever a skill is successfully resolved.
    Failures are logged at DEBUG level and never propagated.
    """
    if not name:
        return
    try:
        with _LOCK:
            data = _load_locked()
            entry = data.setdefault(name, {"load_count": 0, "last_loaded_at": 0.0})
            entry["load_count"] = int(entry.get("load_count", 0)) + 1
            entry["last_loaded_at"] = time.time()
            _evict_if_over_cap_locked(data)
            _save_locked(data)
    except Exception as e:  # noqa: BLE001 — instrumentation must not raise
        logger.debug("skill_usage: record_skill_load failed for %s: %s", name, e)


def get_usage_stats() -> Dict[str, Dict[str, float]]:
    """Return a snapshot of the current usage data (cache-safe)."""
    with _LOCK:
        return {k: dict(v) for k, v in _load_locked().items()}


def reset_cache() -> None:
    """Drop the in-process cache (used by tests when monkeypatching paths)."""
    global _CACHE, _CACHE_PATH
    with _LOCK:
        _CACHE = None
        _CACHE_PATH = None


def score(name: str, *, now: Optional[float] = None) -> float:
    """Return a relevance score for ``name`` (higher = more relevant).

    Combines:
      - Logarithmic load count (heavy use = persistent value)
      - Recency decay with a 7-day half-life

    Both axes contribute multiplicatively so a skill with a single load a
    long time ago doesn't outrank a recently-loaded staple.
    """
    import math
    stats = get_usage_stats().get(name)
    if not stats:
        return 0.0
    load_count = float(stats.get("load_count", 0))
    last = float(stats.get("last_loaded_at", 0))
    if load_count <= 0:
        return 0.0
    now_ts = now if now is not None else time.time()
    age_days = max(0.0, (now_ts - last) / 86400.0) if last > 0 else 30.0
    # 7-day half-life: weight = 0.5 ** (age / 7)
    recency_weight = 0.5 ** (age_days / 7.0)
    # log1p so a skill loaded 100 times doesn't crowd everything else out
    return math.log1p(load_count) * recency_weight
