"""LCM (Long Context Memory) context engine plugin.

Replaces the built-in ContextCompressor's "summarize and discard" approach
with a retrieval-based one:

- Every middle message about to be dropped during compression is embedded
  and stored in a local SQLite database.
- Instead of replacing the middle with an LLM-generated summary, we insert
  a marker telling the agent to use the ``lcm_search`` / ``lcm_recall``
  tools when it needs to look back.
- The agent can pull back specific old turns on demand instead of being
  forced to live with a lossy summary.

Embedding source priority:
  1. Local ``sentence-transformers`` (if installed) — best quality, private
  2. SiliconFlow embeddings API (if main model is on SiliconFlow) — Chinese-friendly
  3. Lexical hash fallback — zero deps, low quality but always works

Storage location: ``$HERMES_HOME/lcm/store.db`` — single DB, ``session_id``
is a column so each session is isolated by ``WHERE session_id = ?``.

Activate via ``~/.hermes/config.yaml``::

    context:
      engine: lcm
      lcm:
        embedder_model: BAAI/bge-m3        # optional — local sentence-transformers
        embedder_device: mps               # optional — auto / cpu / cuda / mps
        prefer_local: true                 # optional — set false to skip tier 1
        embedder_max_seq_length: 4096      # optional — cap input tokens (default 4096)
        embedder_batch_size: 4             # optional — encode batch size
                                           #   (default: cpu=16, mps=4, cuda=16)

Tools exposed to the agent: ``lcm_search`` and ``lcm_recall``.

Tuning notes for the two performance knobs:

- ``embedder_max_seq_length`` defaults to 4096 — well above the engine's
  4000-char chunk size (≈1000-2000 tokens) so chunks always fit verbatim.
  bge-m3 natively supports 8192; raise to 8192 if you want to embed
  larger chunks at the cost of attention memory (RAG benchmarks show
  smaller chunks recall better, so this rarely helps).
- ``embedder_batch_size`` controls the per-encode batch size.  Apple MPS
  is the constrained backend — ``batch=4`` keeps a 24 GB M-series Mac
  comfortable.  A 24 GB CUDA card can push 16-32; a 48 GB+ card can go
  higher if throughput matters.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from .engine import LCMEngine

logger = logging.getLogger(__name__)


def _read_lcm_config() -> dict:
    """Pull ``context.lcm.*`` knobs from ``$HERMES_HOME/config.yaml``.

    Returns an empty dict if the file is missing, malformed, or doesn't
    define LCM-specific overrides — the engine then runs with defaults.
    """
    try:
        from hermes_constants import get_hermes_home  # local import: profile-aware
        cfg_path = get_hermes_home() / "config.yaml"
    except Exception:
        cfg_path = Path.home() / ".hermes" / "config.yaml"

    if not cfg_path.is_file():
        return {}
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001
        logger.debug("LCM register: failed to read %s: %s", cfg_path, e)
        return {}
    ctx_block = raw.get("context") or {}
    return ctx_block.get("lcm") or {}


def register(ctx) -> None:
    """Plugin entry point — Hermes calls this with a context object."""
    cfg = _read_lcm_config()
    kwargs: dict = {}
    if isinstance(cfg, dict):
        if "embedder_model" in cfg:
            kwargs["embedder_model"] = str(cfg["embedder_model"])
        if "embedder_device" in cfg and cfg["embedder_device"]:
            kwargs["embedder_device"] = str(cfg["embedder_device"])
        if "prefer_local" in cfg:
            kwargs["prefer_local_embedder"] = bool(cfg["prefer_local"])
        # Optional performance knobs — see module docstring for tuning.
        if cfg.get("embedder_max_seq_length"):
            try:
                kwargs["embedder_max_seq_length"] = int(cfg["embedder_max_seq_length"])
            except (TypeError, ValueError):
                pass
        if cfg.get("embedder_batch_size"):
            try:
                kwargs["embedder_batch_size"] = int(cfg["embedder_batch_size"])
            except (TypeError, ValueError):
                pass

    engine = LCMEngine(**kwargs)
    if hasattr(ctx, "register_context_engine"):
        ctx.register_context_engine(engine)


__all__ = ["LCMEngine", "register"]
