"""Embedding providers for the LCM context engine.

Tries in order of preference:

1. ``sentence-transformers`` running locally (best quality, zero cost,
   fully private) — only available if the user installed the package.
2. SiliconFlow's embeddings API via the OpenAI SDK (BAAI/bge-large-zh-v1.5
   handles Chinese + English well; the user already has a SiliconFlow
   key configured).
3. Lexical hash bag-of-words fallback — zero dependencies, low quality,
   but always works so the engine never breaks.

All embedders return ``np.float32`` arrays of shape ``(n_texts, dim)``.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import List, Optional

import numpy as np

logger = logging.getLogger(__name__)


class Embedder:
    """Base interface for embedding providers."""

    name: str = "base"
    dim: int = 0

    def embed(self, texts: List[str]) -> np.ndarray:  # pragma: no cover
        raise NotImplementedError


class SentenceTransformerEmbedder(Embedder):
    """Local sentence-transformers embedder. Best when available.

    ``model_name`` accepts any HuggingFace identifier supported by
    ``sentence-transformers`` ≥3.0. Recommended for Chinese/code workloads:

    - ``BAAI/bge-m3`` — SOTA multilingual, 8192-token input, 1024 dim (~2.3GB)
    - ``BAAI/bge-large-zh-v1.5`` — Chinese specialist, 512-token input, 1024 dim (~1.3GB)
    - ``all-MiniLM-L6-v2`` — Tiny English-leaning fallback, 384 dim (~90MB)

    Set the ``model_kwargs`` dict if you need to pass things like
    ``trust_remote_code=True`` for newer models.
    """

    name = "sentence-transformers"

    # Default cap on per-input token length.  bge-m3 advertises 8192 tokens
    # of native context, but two facts make the full 8192 a poor default
    # for an LCM chunk store:
    #
    # 1. Embedding quality.  RAG benchmarks (LangChain, LlamaIndex,
    #    BGE / GTE / e5 papers) consistently find that 256-1024 token
    #    chunks recall better than 4K-8K token chunks once you ask
    #    pinpoint queries — averaging an 8K passage into a 1024-dim
    #    vector dilutes the signal.  LCM compensates with overlapping
    #    char-level segments + neighbour expansion at search time.
    #
    # 2. Attention memory.  ``batch × seq² × hidden`` grows quadratically
    #    in seq_len — a 4-element batch at seq=8192 on bge-m3 needs
    #    ~30 GiB of MPS scratch on Apple silicon, which is exactly why
    #    we were seeing ``Invalid buffer size: 95.28 GiB`` errors.
    #
    # 4096 leaves ~2× headroom over the 4000-char engine segment cap so
    # individual chunks always fit verbatim with room to spare, and the
    # attention footprint becomes tractable even on a 24 GB Mac.
    # Override via ``embedder_max_seq_length`` in config.yaml when you
    # want to push closer to the model's native limit.
    _DEFAULT_MAX_SEQ_LEN = 4096

    # Per-device batch size defaults.  sentence-transformers ships with
    # ``batch_size=32`` which is fine on a 24 GB CUDA card with seq=512
    # but blows up on Apple MPS once seq creeps into the thousands.
    # Tuned against bge-m3 / 1024-dim on an M4 (24 GB) and a CUDA 4090
    # baseline.  Override via ``embedder_batch_size`` in config.yaml.
    _BATCH_SIZE_BY_DEVICE = {
        "cpu": 16,
        "mps": 4,
        "cuda": 16,
    }

    def __init__(
        self,
        model_name: str = "all-MiniLM-L6-v2",
        device: Optional[str] = None,
        model_kwargs: Optional[dict] = None,
        max_seq_length: Optional[int] = None,
        batch_size: Optional[int] = None,
    ):
        from sentence_transformers import SentenceTransformer  # type: ignore

        st_kwargs = dict(model_kwargs or {})
        if device:
            st_kwargs["device"] = device
        self._model = SentenceTransformer(model_name, **st_kwargs)
        # sentence-transformers ≥5.0 renamed the method to
        # ``get_embedding_dimension``; older versions still expose the long
        # form. Try the new name first, fall back to the legacy one.
        if hasattr(self._model, "get_embedding_dimension"):
            self.dim = int(self._model.get_embedding_dimension())
        else:
            self.dim = int(self._model.get_sentence_embedding_dimension())
        self.model_name = model_name

        # Cap input length so MPS / consumer GPUs don't OOM when a chunk
        # slips through unexpectedly.  Strategy:
        #   - if user supplied an explicit override → respect it (modulo
        #     the model's own native cap; we never exceed it)
        #   - otherwise → min(model native, _DEFAULT_MAX_SEQ_LEN)
        # This way models with a small native limit (e.g. MiniLM-L6 at
        # 256) aren't artificially extended, and models with a huge limit
        # (bge-m3 at 8192) get a safe production default of 4096.
        try:
            native = int(getattr(self._model, "max_seq_length", 0) or 0)
            requested = max_seq_length or self._DEFAULT_MAX_SEQ_LEN
            chosen = min(native, requested) if native > 0 else requested
            if chosen > 0:
                self._model.max_seq_length = chosen
            self.max_seq_length = int(
                getattr(self._model, "max_seq_length", chosen) or chosen
            )
        except Exception:
            self.max_seq_length = max_seq_length or self._DEFAULT_MAX_SEQ_LEN

        # Surface the resolved torch device so /lcm status can show
        # whether the user's mps/cuda override actually took effect
        # (sentence-transformers silently falls back to cpu on devices
        # it can't use, and the user has no other way to see that).
        try:
            _resolved_device = next(self._model.parameters()).device
            self.device = str(_resolved_device)
        except Exception:
            self.device = device or "auto"

        # Pick batch size from the device family.  ``mps:0`` etc. → ``mps``.
        device_family = self.device.split(":")[0].lower() if self.device else "cpu"
        self.batch_size = (
            int(batch_size) if batch_size is not None and batch_size > 0
            else self._BATCH_SIZE_BY_DEVICE.get(device_family, 8)
        )

        logger.info(
            "LCM embedder: sentence-transformers/%s (dim=%d, device=%s, "
            "max_seq_length=%d, batch_size=%d)",
            model_name, self.dim, self.device, self.max_seq_length, self.batch_size,
        )

    def embed(self, texts: List[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)

        # NOTE: we do NOT char-truncate here on purpose.
        #
        # Char-level pre-truncation would silently drop the tail of any
        # chunk that the engine sent oversized, which is a quality
        # regression — the LCM engine layer is already responsible for
        # splitting long messages into ``_TOOL_RESULT_SEGMENT_CHARS``
        # (4000-char) overlapping windows, and ``max_seq_length`` above
        # caps the tokenizer at the model's safe operating point, so by
        # the time we get here every input is already small enough to
        # encode.  If a chunk somehow slipped past the engine and
        # exceeds max_seq_length, the model's tokenizer will truncate
        # it cleanly on a token boundary (not mid-character), which
        # preserves more semantic content than naive char slicing.
        safe_texts = [(t if t is not None else "") for t in texts]

        # Encode in small batches so a single oversized batch can't blow
        # up the whole compression call.  If MPS still OOMs (e.g. a model
        # the user swapped in needs more headroom than our defaults), one
        # retry on CPU keeps the chunks index alive instead of dropping
        # the whole batch.
        try:
            vectors = self._model.encode(
                safe_texts,
                batch_size=self.batch_size,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
        except Exception as primary_err:
            primary_msg = str(primary_err)
            if (
                "MPS" in primary_msg
                or "buffer size" in primary_msg
                or "out of memory" in primary_msg.lower()
            ):
                logger.warning(
                    "LCM embed primary backend (%s) failed (%s); retrying on CPU "
                    "with batch_size=2 — consider lowering chunk size or batch "
                    "size in config.yaml (lcm.embedder_batch_size) if this "
                    "happens repeatedly.",
                    self.device, primary_msg.splitlines()[0][:200],
                )
                try:
                    self._model.to("cpu")
                    vectors = self._model.encode(
                        safe_texts,
                        batch_size=2,
                        show_progress_bar=False,
                        convert_to_numpy=True,
                    )
                    # Best-effort: try to move back; if this fails the next
                    # call will just stay on CPU, which is correct.
                    try:
                        self._model.to(self.device)
                    except Exception:
                        pass
                except Exception:
                    raise
            else:
                raise
        return vectors.astype(np.float32, copy=False)


class SiliconFlowEmbedder(Embedder):
    """SiliconFlow API embedder via the OpenAI SDK."""

    name = "siliconflow"

    def __init__(
        self,
        api_key: str,
        model: str = "BAAI/bge-large-zh-v1.5",
        base_url: str = "https://api.siliconflow.cn/v1",
        # bge-large-* outputs 1024 dims; bge-small-* outputs 384.
        dim: int = 1024,
    ):
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model = model
        self.dim = dim
        logger.info("LCM embedder: SiliconFlow %s (dim=%d)", model, dim)

    def embed(self, texts: List[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        # SiliconFlow caps batch size; chunk to be safe.
        out: List[List[float]] = []
        batch_size = 32
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            resp = self._client.embeddings.create(model=self._model, input=batch)
            out.extend([d.embedding for d in resp.data])
        return np.asarray(out, dtype=np.float32)


# Tiny English+Chinese-aware tokenizer for the lexical fallback.
# Splits on word boundaries and treats each CJK character as its own token.
_LEXICAL_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


class LexicalEmbedder(Embedder):
    """Hash-based bag-of-words embedder. Last-resort fallback.

    Splits text into ASCII words plus per-character CJK tokens, hashes
    each token into a fixed-size vector, then L2-normalises. Works for
    keyword-overlap retrieval; obviously inferior to a real embedding
    model but never fails to load.
    """

    name = "lexical-hash"

    def __init__(self, dim: int = 256):
        self.dim = dim
        logger.info("LCM embedder: lexical hash fallback (dim=%d)", dim)

    def embed(self, texts: List[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            tokens = _LEXICAL_TOKEN_RE.findall((text or "").lower())
            for tok in tokens:
                idx = (
                    int(hashlib.md5(tok.encode("utf-8")).hexdigest()[:8], 16)
                    % self.dim
                )
                out[i, idx] += 1.0
            norm = float(np.linalg.norm(out[i]))
            if norm > 0.0:
                out[i] /= norm
        return out


def get_default_embedder(
    siliconflow_api_key: Optional[str] = None,
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1",
    siliconflow_model: str = "BAAI/bge-large-zh-v1.5",
    sentence_transformer_model: str = "all-MiniLM-L6-v2",
    sentence_transformer_device: Optional[str] = None,
    prefer_local: bool = True,
    sentence_transformer_max_seq_length: Optional[int] = None,
    sentence_transformer_batch_size: Optional[int] = None,
) -> Embedder:
    """Pick the best available embedder.

    Default order: local sentence-transformers → SiliconFlow API → lexical hash.

    Set ``prefer_local=False`` to skip tier 1 entirely (useful when the user
    explicitly wants the cloud embedder even though both are available).

    ``sentence_transformer_max_seq_length`` / ``sentence_transformer_batch_size``
    expose the per-input token cap and per-encode batch size for the local
    embedder.  Both are optional — defaults (4096 / device-aware) are tuned
    to avoid Apple MPS OOM on bge-m3 while preserving recall quality.
    Override when running on bigger CUDA cards or when you want to push
    closer to the model's native limit.
    """
    if prefer_local:
        try:
            return SentenceTransformerEmbedder(
                sentence_transformer_model,
                device=sentence_transformer_device,
                max_seq_length=sentence_transformer_max_seq_length,
                batch_size=sentence_transformer_batch_size,
            )
        except ImportError:
            logger.info(
                "LCM: sentence-transformers not installed; "
                "falling back to SiliconFlow API. "
                "(Run `pip install sentence-transformers` to use %s locally.)",
                sentence_transformer_model,
            )
        except Exception as e:  # noqa: BLE001 — defensive on init failure
            logger.warning(
                "LCM: failed to load sentence-transformers/%s (%s); "
                "falling back to SiliconFlow",
                sentence_transformer_model, e,
            )

    if siliconflow_api_key:
        try:
            return SiliconFlowEmbedder(
                api_key=siliconflow_api_key,
                model=siliconflow_model,
                base_url=siliconflow_base_url,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("LCM: SiliconFlow embedder init failed: %s", e)

    logger.warning(
        "LCM: falling back to lexical hash embedder. Install "
        "`sentence-transformers` (pip install sentence-transformers) for "
        "much better recall, or configure a SiliconFlow API key."
    )
    return LexicalEmbedder()
