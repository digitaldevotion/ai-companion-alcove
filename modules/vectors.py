#!/usr/bin/env python3
# ============================================
# Alcove — vectors.py
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import chromadb


def _get_embedding_model_name():
    """Read the pinned embedding model name from main.py engine globals."""
    try:
        from . import main
        return getattr(main, "SEARCH_REFERENCES_EMBEDDING_MODEL", "bge-m3-onnx")
    except Exception:
        return "bge-m3-onnx"


# ── bge-m3 ONNX embedding function ────────────────────────────────────────────
# Replaces ChromaDB's DefaultEmbeddingFunction (onnx-miniLM-L6-v2: 256-token
# max sequence, 384-dim, English-biased) with BAAI/bge-m3 (8192-token max
# sequence, 1024-dim, 100+ languages incl. CJK). The old model silently
# truncated chunks at 256 tokens, leaving ~49–87% of each chunk invisible
# to vector search. bge-m3's 8192-token limit comfortably covers the 2000-char
# chunk size (~500–2000 tokens) without truncation.
#
# The model is downloaded by utils/install_deps.py to
# ~/.cache/chroma/onnx_models/bge-m3/ (co-located with the old
# all-MiniLM-L6-v2 cache). Implements the ChromaDB EmbeddingFunction protocol
# (__call__ + name). Uses pure ONNX runtime — no torch dependency (~50MB of
# new pip deps vs ~2GB for sentence-transformers).

_BGE_M3_MODEL_DIR = os.path.expanduser("~/.cache/chroma/onnx_models/bge-m3")
_BGE_M3_MAX_SEQ_LEN = 8192
_BGE_M3_EMBED_DIM = 1024
_BGE_M3_BATCH_SIZE = 2   # small batches for frequent progress feedback; bge-m3 on CPU is ~0.5-1s/chunk


class BgeM3OnnxEmbeddingFunction:
    """ChromaDB embedding function for BAAI/bge-m3 via ONNX runtime.

    Lazy-loads the ONNX model and tokenizer on first call (not at module
    import time) so the bot starts even if the model hasn't been downloaded
    yet — the error surfaces when vector search is actually attempted, with
    a clear message pointing to install_deps.py.
    """

    def __init__(self):
        self._session = None
        self._tokenizer = None
        self._np = None
        self._input_names = None
        self._output_name = None

    def _ensure_loaded(self):
        """Lazy-load the ONNX session and tokenizer on first use."""
        if self._session is not None:
            return

        import onnxruntime as ort
        import numpy as np
        from tokenizers import Tokenizer

        model_path = os.path.join(_BGE_M3_MODEL_DIR, "model.onnx")
        tokenizer_path = os.path.join(_BGE_M3_MODEL_DIR, "tokenizer.json")

        if not os.path.exists(model_path):
            raise FileNotFoundError(
                f"bge-m3 ONNX model not found at {model_path}. "
                f"Run `python3 utils/install_deps.py` to download it "
                f"(~2.3GB, first install only)."
            )
        if not os.path.exists(tokenizer_path):
            raise FileNotFoundError(
                f"bge-m3 tokenizer not found at {tokenizer_path}. "
                f"Run `python3 utils/install_deps.py` to download it."
            )

        # Load tokenizer (xlm-roberta SentencePiece BPE). enable_padding
        # uses the tokenizer's configured pad_id (xlm-roberta pad_id=1).
        # enable_truncation caps at 8192 — chunks of ~2000 chars (~500–2000
        # tokens) are well under this limit, so no content is lost.
        tokenizer = Tokenizer.from_file(tokenizer_path)
        tokenizer.enable_truncation(max_length=_BGE_M3_MAX_SEQ_LEN)
        tokenizer.enable_padding()

        # Create ONNX session. Prefer CoreML on macOS (Apple Neural Engine /
        # GPU — 5-10x faster than CPU for a 568M-param model), fall back to
        # CPU on Linux/Windows/Raspberry Pi. CoreML is only available on
        # macOS; on other platforms the provider isn't in the list so ONNX
        # Runtime falls through to CPU automatically — no platform-specific
        # code needed.
        #
        # NOTE: CoreML doesn't support ONNX external data format (the
        # 2.27GB model.onnx_data file) — it fails at session creation with
        # "Failed to get file size for external initializer". Detect this
        # upfront and skip CoreML when external data is present, avoiding
        # a noisy exception + fallback on every startup.
        sess_options = ort.SessionOptions()
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # Parallelize matrix ops across all available cores. On CPU this
        # gives ~1.5-2x speedup; CoreML manages its own threading but the
        # config is harmless when CoreML is active.
        sess_options.intra_op_num_threads = os.cpu_count() or 4
        sess_options.inter_op_num_threads = 4

        # Detect external data format: if model.onnx_data exists alongside
        # model.onnx, CoreML will fail — go straight to CPU.
        external_data_path = os.path.join(_BGE_M3_MODEL_DIR, "model.onnx_data")
        has_external_data = os.path.exists(external_data_path)
        if has_external_data:
            preferred_providers = ["CPUExecutionProvider"]
        else:
            preferred_providers = ["CoreMLExecutionProvider", "CPUExecutionProvider"]

        session = None
        _active_provider = "CPUExecutionProvider"
        try:
            session = ort.InferenceSession(
                model_path,
                sess_options=sess_options,
                providers=preferred_providers,
            )
            _active_provider = session.get_providers()[0] if session.get_providers() else "CPUExecutionProvider"
        except Exception as _provider_err:
            # Provider failed (e.g. CoreML issue we didn't detect). Fall
            # back to CPU-only and log the fallback.
            print(f"  -- ONNX provider failed ({_provider_err}); falling back to CPU")
            session = ort.InferenceSession(
                model_path,
                sess_options=sess_options,
                providers=["CPUExecutionProvider"],
            )
            _active_provider = "CPUExecutionProvider"

        print(f"  🧠 ONNX runtime active provider: {_active_provider} "
              f"({os.cpu_count() or 'unknown'} CPU cores available)")

        self._session = session
        self._tokenizer = tokenizer
        self._np = np
        self._input_names = [i.name for i in session.get_inputs()]
        self._output_name = session.get_outputs()[0].name

    def __call__(self, input):
        """Embed a list of documents. Returns list[list[float]] (1024-dim each).

        ChromaDB calls this when adding documents to the collection and when
        querying. The input is a list of strings (documents or query text).
        """
        if not input:
            return []

        self._ensure_loaded()
        np = self._np
        all_embeddings = []

        # Process in batches capped at _BGE_M3_BATCH_SIZE to avoid OOM on
        # small hosts. The tokenizer pads each batch to its longest sequence,
        # so memory scales with the longest chunk in the batch, not with
        # _BGE_M3_MAX_SEQ_LEN.
        for start in range(0, len(input), _BGE_M3_BATCH_SIZE):
            batch = list(input[start:start + _BGE_M3_BATCH_SIZE])
            encodings = self._tokenizer.encode_batch(batch)
            input_ids = np.array([enc.ids for enc in encodings], dtype=np.int64)
            attention_mask = np.array([enc.attention_mask for enc in encodings], dtype=np.int64)

            # Build feed dict from the model's actual input names. bge-m3's
            # ONNX export uses input_ids + attention_mask; token_type_ids is
            # included defensively in case the export adds it.
            feed = {}
            for name in self._input_names:
                if name == "input_ids":
                    feed[name] = input_ids
                elif name == "attention_mask":
                    feed[name] = attention_mask
                elif name == "token_type_ids":
                    feed[name] = np.zeros_like(input_ids)
                else:
                    raise RuntimeError(
                        f"bge-m3 ONNX has unexpected input '{name}' — "
                        f"the model export may have changed."
                    )

            outputs = self._session.run(None, feed)
            last_hidden_state = outputs[0]  # [batch, seq_len, 1024]

            # Mean pool by attention mask, then L2 normalize. This is the
            # standard sentence-transformers pooling for BGE models.
            mask = attention_mask[:, :, np.newaxis]  # [batch, seq_len, 1]
            summed = (last_hidden_state * mask).sum(axis=1)  # [batch, 1024]
            counts = mask.sum(axis=1).clip(min=1e-9)  # [batch, 1]
            mean = summed / counts  # [batch, 1024]

            norms = np.linalg.norm(mean, axis=1, keepdims=True)
            normalized = mean / np.maximum(norms, 1e-12)

            all_embeddings.extend(normalized.tolist())

        return all_embeddings

    def embed_documents(self, documents):
        """Embed a list of documents. Called by ChromaDB on Collection.add().

        ChromaDB's Collection._embed dispatches to embed_documents() for
        document ingestion (is_query=False). __call__ is the lower-level
        entry point but is NOT what Collection.add() invokes for a custom
        (non-DefaultEmbeddingFunction) embedding function.
        """
        return self.__call__(documents)

    def embed_query(self, input):
        """Embed query text for similarity search. Called by ChromaDB on
        Collection.query().

        ChromaDB's Collection._embed dispatches to embed_query() when
        is_query=True. ChromaDB normalizes a single query string into a
        list before calling this, so `input` is always list[str]. Returns
        list[list[float]] (one embedding per input string), matching the
        contract verified against DefaultEmbeddingFunction.
        """
        return self.__call__(input)

    def name(self):
        return "bge-m3-onnx"

# ── Constants ────────────────────────────────────────────────────────────────

_COLLECTION_NAME = "search_references"
_CHUNK_OVERLAP = 100    # overlap chars between consecutive chunks

_client = None
_collection = None
_chunk_size = 2000  # stored from init, used to estimate results for budget-based queries
_active_companion_name = None  # tracked dynamically to detect companion switches
_keyword_cache = None  # {"chunks": {(source, idx): doc}, "index": {word: set of (source, idx)}}


# ── Internal helpers ─────────────────────────────────────────────────────────

def _get_paths(companion_name):
    """Dynamically resolve persistent data paths for the active companion."""
    data_dir = Path(__file__).parent.parent / "databases" / companion_name / "search_vectors_data"
    manifest_file = data_dir / "manifest.txt"
    return data_dir, manifest_file

def _file_signature(path):
    """Return a hash of file mtime + size for change detection."""
    try:
        st = os.stat(path)
        raw = f"{path}|{st.st_mtime}|{st.st_size}"
        return hashlib.md5(raw.encode()).hexdigest()
    except OSError:
        return None


def _load_manifest(companion_name):
    """Load the manifest dict (filepath -> signature) from disk.

    Returns (manifest_dict, stored_model_name). The stored_model_name is
    the embedding model that was active when the collection was last built;
    the caller compares it to the current pinned model to decide whether
    a full rebuild is needed. Returns (dict, None) for old manifests that
    predate the model signature line.
    """
    _, manifest_file = _get_paths(companion_name)
    manifest = {}
    stored_model = None
    if manifest_file.exists():
        for line in manifest_file.read_text().splitlines():
            if "|" not in line:
                continue
            sig, fp = line.split("|", 1)
            if fp == "__model__":
                stored_model = sig
                continue
            manifest[fp] = sig
    return manifest, stored_model


def _save_manifest(manifest, companion_name):
    """Write the manifest dict to disk, including the embedding model name."""
    data_dir, manifest_file = _get_paths(companion_name)
    data_dir.mkdir(parents=True, exist_ok=True)
    model_name = _get_embedding_model_name()
    lines = [f"__model__|{model_name}"]
    lines.extend(f"{sig}|{fp}" for fp, sig in manifest.items())
    manifest_file.write_text("\n".join(lines) + "\n")


def _chunk_text(text, chunk_size, overlap=_CHUNK_OVERLAP):
    """Split text into overlapping chunks, preferring paragraph/sentence breaks."""
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        if end >= len(text):
            chunks.append(text[start:].strip())
            break

        # Try to break at a paragraph boundary
        para = text.rfind("\n\n", start + chunk_size // 2, end)
        if para != -1:
            end = para + 2
        else:
            # Try to break at a sentence boundary
            sent = re.search(r'[.!?]\s', text[start + chunk_size // 2:end])
            if sent:
                end = start + chunk_size // 2 + sent.end()

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start = end - overlap if end - start > overlap else end

    return chunks


def _read_file(path):
    """Read a file's text content, returning empty string on failure."""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _build_keyword_cache():
    """Load the full collection and build an inverted index for keyword lookups."""
    global _keyword_cache
    all_data = _collection.get(include=["documents", "metadatas"])
    all_chunks = {}
    inverted_index = {}
    for doc, meta in zip(all_data["documents"], all_data["metadatas"]):
        source = meta.get("source_file", "unknown")
        idx = meta.get("chunk_index", 0)
        key = (source, idx)
        all_chunks[key] = doc
        for word in set(re.findall(r"\w+", doc.lower())):
            if word not in inverted_index:
                inverted_index[word] = set()
            inverted_index[word].add(key)
    _keyword_cache = {"chunks": all_chunks, "index": inverted_index}


def _invalidate_keyword_cache():
    global _keyword_cache
    _keyword_cache = None


# ── Public API ───────────────────────────────────────────────────────────────

def _run_init_in_subprocess(reference_files, chunk_size, companion_name):
    """Run _init_vector_store_inner in a subprocess to avoid GIL contention.

    The ONNX InferenceSession constructor (loading the 2.1GB bge-m3 model)
    holds the GIL for the entire duration — ~3s on warm cache, potentially
    much longer on cold cache. When run in a thread, this blocks the asyncio
    event loop on the main thread, making the bot unresponsive. A subprocess
    has its own GIL, so the main process's event loop is never blocked.

    The subprocess writes directly to ChromaDB's on-disk format (SQLite +
    vector data). After it finishes, the caller opens the collection in the
    main process for queries.

    Progress prints from the subprocess inherit the parent's stdout/stderr,
    so the user sees the same real-time embedding progress as before.
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    script = (
        "import sys, os, json\n"
        f"sys.path.insert(0, {project_root!r})\n"
        "from modules.vectors import _init_vector_store_inner\n"
        "args = json.loads(os.environ['ALCOVE_VECTOR_INIT_ARGS'])\n"
        "_init_vector_store_inner(args['files'], args['chunk_size'], args['companion'])\n"
    )

    env = os.environ.copy()
    env['ALCOVE_VECTOR_INIT_ARGS'] = json.dumps({
        'files': [str(p) for p in reference_files],
        'chunk_size': chunk_size,
        'companion': companion_name,
    })

    result = subprocess.run(
        [sys.executable, '-u', '-c', script],
        env=env,
        cwd=project_root,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"vector init subprocess exited with code {result.returncode}"
        )


def init_vector_store(reference_files, chunk_size=2000, companion_name="default",
                      block_search=True):
    """Initialize or reopen the ChromaDB vector store and sync with reference_files.

    - Embeds new or changed chunks (per-chunk checksum diffing)
    - Removes chunks for files no longer in reference_files
    - Skips unchanged files (manifest-based change detection)

    Thread-safe: acquires state._vector_init_lock so concurrent callers
    (boot, !switchCompanion, auto_discover_paths_task, search.py's dynamic
    companion-mismatch reload) serialize — two inits never overlap and
    corrupt ChromaDB state.

    block_search: when True (default, used by boot/cold-init/migration paths),
    sets state.SEARCH_INITIALIZING for the duration so search.inject_search_context
    skips the search step and keeps the bot responsive to prompts during long
    embeddings. When False (used by the periodic auto_discover_paths_task
    refresh), the flag is NOT set — search keeps using the existing vectors in
    the database while changed/new chunks are embedded in the background. This
    avoids blocking search during the routine 15-minute refresh, since the
    collection already has usable vectors.

    The heavy embedding work (ONNX model load + per-chunk inference + ChromaDB
    writes) runs in a SUBPROCESS to avoid GIL contention with the main
    process's asyncio event loop. After the subprocess finishes, the main
    process opens the ChromaDB collection for queries (lightweight — SQLite
    open + metadata read, no ONNX model load; the model loads lazily on the
    first search query).
    """
    global _client, _collection, _chunk_size, _active_companion_name

    from . import state
    _ref_list = list(reference_files)
    print(f"🔎 vectors: starting init for companion '{companion_name}' "
          f"({len(_ref_list)} reference file(s)) — running in subprocess"
          f"{' (non-blocking, search stays live)' if not block_search else ''}")
    with state._vector_init_lock:
        if block_search:
            state.SEARCH_INITIALIZING = True
        # Invalidate the in-memory keyword cache up front for non-blocking
        # refreshes so searches during the refresh rebuild it from the live
        # collection (tolerating momentary inconsistency) instead of using
        # stale data while chunks are deleted/added.
        if not block_search:
            _invalidate_keyword_cache()
        try:
            _run_init_in_subprocess(_ref_list, chunk_size, companion_name)

            # Open the ChromaDB collection in the main process for queries.
            # This is lightweight: PersistentClient opens the SQLite DB,
            # get_or_create_collection reads collection metadata. No ONNX model
            # load here — the model loads lazily on the first search query.
            data_dir, _ = _get_paths(companion_name)
            _chunk_size = chunk_size
            _ef = BgeM3OnnxEmbeddingFunction()
            _client = chromadb.PersistentClient(path=str(data_dir))
            _collection = _client.get_or_create_collection(
                name=_COLLECTION_NAME,
                embedding_function=_ef,
                metadata={"hnsw:space": "cosine"},
            )
            _active_companion_name = companion_name
        except Exception as e:
            print(f"⚠️ vector store init failed for companion '{companion_name}' (search will be unavailable): {e}")
            _collection = None
            _active_companion_name = None
        finally:
            if block_search:
                state.SEARCH_INITIALIZING = False
    # Invalidate again at the end so the cache reflects the freshly written
    # chunks regardless of which path was taken.
    _invalidate_keyword_cache()


def _init_vector_store_inner(reference_files, chunk_size, companion_name):
    """Internal implementation of init_vector_store. Errors are caught by the caller."""
    global _client, _collection, _chunk_size

    _chunk_size = chunk_size
    data_dir, _ = _get_paths(companion_name)
    data_dir.mkdir(parents=True, exist_ok=True)

    _model_name = _get_embedding_model_name()
    _ef = BgeM3OnnxEmbeddingFunction()
    _client = chromadb.PersistentClient(path=str(data_dir))

    manifest, stored_model = _load_manifest(companion_name)

    # If the embedding model has changed since the collection was last built,
    # wipe and rebuild from scratch — the persisted vectors are in a different
    # embedding space and would produce silently broken search results.
    # Backward compat: if stored_model is None (old manifest without the
    # __model__ line), we can't tell from the manifest whether the collection
    # matches. The get_or_create_collection retry below handles that case by
    # catching an embedding-function conflict and wiping then.
    if stored_model is not None and stored_model != _model_name:
        print(f"⚠️ Embedding model changed ('{stored_model}' → '{_model_name}') "
              f"— rebuilding vector store from scratch for companion '{companion_name}'.")
        print(f"   ⏳ This may take a few minutes depending on how much reference data you have "
              f"and your hardware. Progress will be shown below.")
        try:
            _client.delete_collection(name=_COLLECTION_NAME)
        except Exception:
            pass
        manifest = {}

    # get_or_create_collection will raise if the collection already exists
    # with a different embedding function (e.g. an old install built with
    # ChromaDB's DefaultEmbeddingFunction that has no __model__ manifest
    # line, so the manifest check above couldn't detect the mismatch). Catch
    # the conflict, wipe, and retry — the rebuild is always safe because the
    # old vectors are in an incompatible embedding space.
    try:
        _collection = _client.get_or_create_collection(
            name=_COLLECTION_NAME,
            embedding_function=_ef,
            metadata={"hnsw:space": "cosine"},
        )
    except Exception as _coll_err:
        _msg = str(_coll_err)
        if "embedding function" in _msg.lower() and "conflict" in _msg.lower():
            print(f"⚠️ Embedding function conflict detected — wiping old "
                  f"collection and rebuilding for companion '{companion_name}'.")
            print(f"   ⏳ This may take a few minutes depending on how much reference data you have "
                  f"and your hardware. Progress will be shown below.")
            try:
                _client.delete_collection(name=_COLLECTION_NAME)
            except Exception:
                pass
            manifest = {}
            _collection = _client.get_or_create_collection(
                name=_COLLECTION_NAME,
                embedding_function=_ef,
                metadata={"hnsw:space": "cosine"},
            )
        else:
            raise

    current_files = {str(p) for p in reference_files}

    # ── Remove stale chunks (files no longer in reference_files) ──
    existing_meta = _collection.get(include=["metadatas"])
    if existing_meta["ids"]:
        source_files_in_db = set()
        for meta in existing_meta["metadatas"]:
            sf = meta.get("source_file", "")
            source_files_in_db.add(sf)

        stale_sources = source_files_in_db - current_files
        if stale_sources:
            for stale_src in stale_sources:
                stale_ids = [
                    mid for mid, meta in zip(existing_meta["ids"], existing_meta["metadatas"])
                    if meta.get("source_file") == stale_src
                ]
                if stale_ids:
                    _collection.delete(ids=stale_ids)
                    print(f"  🗑️  vectors: removed stale chunks for {stale_src} "
                          f"({len(stale_ids)} chunk(s))")
            # Remove stale entries from manifest
            manifest = {fp: sig for fp, sig in manifest.items() if fp in current_files}

    # ── Embed new or changed files (chunk-level diffing) ──
    # First pass: identify which files need examination so we can show a
    # progress meter (file X of Y). Reading + chunking is cheap relative
    # to the ONNX embedding call, so we chunk upfront to get an accurate
    # chunk count. A file is examined when its file-signature (mtime+size)
    # differs from the manifest — but within an examined file, individual
    # chunks are only re-embedded when their chunk_checksum (md5 of the
    # chunk text) differs from the stored value, so a small edit to a
    # large file only re-embeds the changed chunks instead of the whole
    # file. Unchanged chunks are left untouched and never logged.
    files_to_embed = []
    skipped = 0
    for filepath in sorted(current_files):
        sig = _file_signature(filepath)
        if sig is None:
            print(f"  ⚠️  vectors: cannot stat {filepath}, skipping")
            continue
        if manifest.get(filepath) == sig:
            skipped += 1
            continue
        text = _read_file(filepath)
        if not text.strip():
            continue
        chunks = _chunk_text(text, chunk_size=chunk_size)
        if not chunks:
            continue
        files_to_embed.append((filepath, sig, chunks))

    embedded_files = 0
    total_chunks_reembedded = 0
    _embed_start = time.time()

    if files_to_embed:
        print(f"  📊 vectors: {len(files_to_embed)} file(s) to examine, "
              f"{skipped} unchanged file(s) skipped")

    for file_idx, (filepath, sig, chunks) in enumerate(files_to_embed, 1):
        _basename = os.path.basename(filepath)
        _num_chunks = len(chunks)
        _file_start = time.time()

        # Compute checksums for the new chunking so we can diff against
        # the stored per-chunk checksums in ChromaDB metadata.
        new_checksums = [hashlib.md5(c.encode("utf-8")).hexdigest() for c in chunks]
        new_indices = set(range(_num_chunks))

        # Fetch existing chunks for this file so we can diff per chunk
        # rather than re-embedding the whole file. where= filters on
        # source_file, which every chunk carries.
        existing_for_file = _collection.get(
            where={"source_file": filepath},
            include=["metadatas"],
        )
        existing_by_index = {}  # chunk_index -> (id, stored_checksum)
        for mid, meta in zip(existing_for_file["ids"], existing_for_file["metadatas"]):
            idx = meta.get("chunk_index")
            if idx is None:
                continue
            existing_by_index[idx] = (mid, meta.get("chunk_checksum"))

        # Classify each new chunk index against the stored checksum.
        # A missing stored checksum (old chunk predating this feature)
        # is treated as changed, so the checksum gets backfilled on the
        # file's first change after this code ships.
        ids_to_delete = []
        chunks_to_embed = []  # list of (index, text, checksum)
        _new_count = 0
        _changed_count = 0
        _unchanged_in_file = 0
        for i, (text_chunk, chk) in enumerate(zip(chunks, new_checksums)):
            existing = existing_by_index.get(i)
            if existing is None:
                chunks_to_embed.append((i, text_chunk, chk))
                _new_count += 1
            elif existing[1] == chk:
                _unchanged_in_file += 1
            else:
                ids_to_delete.append(existing[0])
                chunks_to_embed.append((i, text_chunk, chk))
                _changed_count += 1

        # Chunks that existed in the DB but are no longer in the file
        # (file shrank, or chunking shifted). Delete by id; no re-embed.
        _removed_count = 0
        for i in existing_by_index:
            if i not in new_indices:
                ids_to_delete.append(existing_by_index[i][0])
                _removed_count += 1

        if ids_to_delete:
            _collection.delete(ids=ids_to_delete)

        # Embed only the changed/new chunks in sub-batches so we can
        # print chunk-level progress within each file. We call the
        # embedding function directly (_ef.embed_documents), then pass
        # the pre-computed embeddings to _collection.add(embeddings=...)
        # so ChromaDB doesn't re-embed. Unchanged chunks skip this entirely.
        all_ids = []
        all_docs = []
        all_metas = []
        all_embeddings = []

        # Print progress every _PROGRESS_EVERY_CHUNKS chunks, or on the
        # last batch of each file. Kept low (every 1-2 chunks) because
        # bge-m3 on CPU is slow (~0.5-1s per chunk) and the user needs
        # frequent feedback that the process is alive.
        _PROGRESS_EVERY_CHUNKS = 2
        _chunks_done_in_file = 0
        _last_printed_at = 0
        _batch_timer = time.time()

        for batch_start in range(0, len(chunks_to_embed), _BGE_M3_BATCH_SIZE):
            batch_end = min(batch_start + _BGE_M3_BATCH_SIZE, len(chunks_to_embed))
            batch = chunks_to_embed[batch_start:batch_end]
            batch_texts = [b[1] for b in batch]
            batch_ids = [f"{filepath}::chunk{b[0]}" for b in batch]
            batch_metas = [{"source_file": filepath,
                            "chunk_index": b[0],
                            "chunk_checksum": b[2]} for b in batch]

            batch_embeddings = _ef.embed_documents(batch_texts)

            all_ids.extend(batch_ids)
            all_docs.extend(batch_texts)
            all_metas.extend(batch_metas)
            all_embeddings.extend(batch_embeddings)

            _chunks_done_in_file += len(batch)
            _is_last_batch = batch_end >= len(chunks_to_embed)
            if (_chunks_done_in_file - _last_printed_at >= _PROGRESS_EVERY_CHUNKS
                    or _is_last_batch):
                _batch_elapsed = time.time() - _batch_timer
                _chunks_in_batch = _chunks_done_in_file - _last_printed_at
                _per_chunk = _batch_elapsed / max(_chunks_in_batch, 1)
                print(f"  📄 vectors: [{file_idx}/{len(files_to_embed)}] {_basename} "
                      f"— embedding {_chunks_done_in_file}/{len(chunks_to_embed)} "
                      f"({_per_chunk:.1f}s/chunk)")
                _last_printed_at = _chunks_done_in_file
                _batch_timer = time.time()

        if all_ids:
            _collection.add(
                ids=all_ids,
                documents=all_docs,
                metadatas=all_metas,
                embeddings=all_embeddings,
            )

        manifest[filepath] = sig
        embedded_files += 1
        total_chunks_reembedded += len(chunks_to_embed)
        _file_elapsed = time.time() - _file_start
        _reembedded = len(chunks_to_embed)
        print(f"  ✅ vectors: [{file_idx}/{len(files_to_embed)}] {_basename} "
              f"done — {_changed_count} re-embedded, {_new_count} new, "
              f"{_removed_count} removed, {_unchanged_in_file} unchanged "
              f"in {_file_elapsed:.1f}s")

    _save_manifest(manifest, companion_name)

    total = _collection.count()
    if embedded_files:
        _elapsed = time.time() - _embed_start
        print(f"  🔎 vectors: store ready — {total} chunks total "
              f"({embedded_files} file(s) examined, "
              f"{total_chunks_reembedded} chunk(s) re-embedded, "
              f"{skipped} unchanged) "
              f"in {_elapsed:.1f}s")
    else:
        print(f"  🔎 vectors: store ready — {total} chunks total "
              f"({embedded_files} file(s) examined, {skipped} unchanged)")


def search_vectors(query, max_results=5, max_chars=0, maximize_context=False, max_distance=0.8, keyword_selectivity=0.10):
    """Hybrid search: keyword pass (selective exact matches) + vector pass
    (semantic neighbors). Returns (result_text, chunk_count, raw_chunks,
    raw_chars, collection_total, relevant_count), or ("", 0, 0, 0, 0, 0).

    Keyword pass: splits the query into words and does a case-insensitive
    match against all chunks. Words that match more than keyword_selectivity
    fraction of the collection are considered too common (poor discriminators)
    and their chunks are only kept if the chunk also matches at least one
    selective keyword.

    Vector pass: queries ChromaDB embeddings and filters by max_distance.
    Catches semantically related chunks the keywords would miss (e.g.,
    searching "bird" finds "colorful parrot").

    Results are merged: keyword matches first (authoritative), then
    vector-only matches, deduplicated by (source_file, chunk_index).
    max_results=0 means determine automatically. max_chars=0 means unlimited.
    """
    try:
        if _collection is None or _collection.count() == 0:
            return "", 0, 0, 0, 0, 0
        return _search_vectors_inner(
            query, max_results, max_chars, maximize_context,
            max_distance, keyword_selectivity,
        )
    except Exception as e:
        print(f"⚠️ vector search failed (non-fatal): {e}")
        return "", 0, 0, 0, 0, 0


def _search_vectors_inner(query, max_results, max_chars, maximize_context, max_distance, keyword_selectivity):
    """Internal implementation of search_vectors. Errors are caught by the caller."""
    collection_total = _collection.count()

    # ── Keyword pass ────────────────────────────────────────────────────────
    # Extract individual search words from the query, filtering out very
    # short or common words that would match almost everything.
    # _STOP_WORDS commented out — extract_search_keywords already sends concise
    # terms, and the keyword_selectivity filter demotes low-value matches
    # regardless. Re-enable if needed.
    # _STOP_WORDS = {
    #     "a", "an", "the", "and", "or", "but", "is", "are", "was", "were",
    #     "be", "been", "being", "have", "has", "had", "do", "does", "did",
    #     "will", "would", "could", "should", "may", "might", "shall", "can",
    #     "to", "of", "in", "for", "on", "with", "at", "by", "from", "as",
    #     "into", "through", "during", "before", "after", "above", "below",
    #     "between", "out", "off", "over", "under", "again", "further",
    #     "then", "once", "here", "there", "when", "where", "why", "how",
    #     "all", "each", "every", "both", "few", "more", "most", "other",
    #     "some", "such", "no", "nor", "not", "only", "own", "same", "so",
    #     "than", "too", "very", "just", "because", "if", "about", "up",
    #     "it", "its", "i", "me", "my", "we", "us", "our", "you", "your",
    #     "he", "him", "his", "she", "her", "they", "them", "their", "this",
    #     "that", "these", "those", "what", "which", "who", "whom",
    # }
    query_words = [
        w for w in re.split(r"\s+", query.strip())
        if len(w) >= 3  # and w.lower() not in _STOP_WORDS
    ]

    keyword_hits = {}  # (source_file, chunk_index) -> document_text
    if query_words:
        if _keyword_cache is None:
            _build_keyword_cache()
        all_chunks = _keyword_cache["chunks"]
        inverted_index = _keyword_cache["index"]

        word_match_counts = {}
        word_patterns = {}
        for w in query_words:
            pattern = re.compile(re.escape(w), re.IGNORECASE)
            word_patterns[w] = pattern
            candidates = inverted_index.get(w.lower(), set())
            count = sum(1 for key in candidates if pattern.search(all_chunks[key]))
            word_match_counts[w] = count

        max_common_hits = collection_total * keyword_selectivity
        selective_words = {w for w, c in word_match_counts.items() if c <= max_common_hits}
        common_words = {w for w, c in word_match_counts.items() if c > max_common_hits}

        if common_words:
            print(f"🔎 keyword pass: common words dropped (>{keyword_selectivity:.0%} of "
                  f"{collection_total}): "
                  + ", ".join(f"{w!r} ({word_match_counts[w]})" for w in common_words))

        word_chunk_sets = {}
        selective_scores = {}
        for w in query_words:
            candidates = inverted_index.get(w.lower(), set())
            pattern = word_patterns[w]
            for key in candidates:
                if pattern.search(all_chunks[key]):
                    if w not in word_chunk_sets:
                        word_chunk_sets[w] = set()
                    word_chunk_sets[w].add(key)
                    if w in selective_words:
                        selective_scores[key] = selective_scores.get(key, 0) + 1

        # Keep chunks that match at least one selective keyword.
        # Chunks that ONLY match common keywords are dropped.
        kept_keys = set()
        for w in selective_words:
            if w in word_chunk_sets:
                kept_keys.update(word_chunk_sets[w])

        # Also keep chunks that match BOTH a common keyword AND a selective one
        # (they're already in kept_keys from the selective word above).
        # Chunks matching ONLY common words are excluded.

        # Cap keyword hits when not maximizing context (mirrors vector pass cap)
        keyword_cap = max(15, collection_total * 15 // 100) if not maximize_context else 0
        if keyword_cap and len(kept_keys) > keyword_cap:
            sorted_keys = sorted(kept_keys, key=lambda k: selective_scores.get(k, 0), reverse=True)
            kept_keys = set(sorted_keys[:keyword_cap])
            print(f"🔎 keyword pass: capped at {keyword_cap} (15% of {collection_total}), "
                  f"trimmed {len(sorted_keys) - keyword_cap} lowest-scoring chunks")

        for key in kept_keys:
            keyword_hits[key] = all_chunks[key]

        # Deduplicate for the "before" count
        all_matching_keys = set()
        for s in word_chunk_sets.values():
            all_matching_keys.update(s)

        print(f"🔎 keyword pass: {len(query_words)} search word(s), "
              f"{len(selective_words)} selective / {len(common_words)} common → "
              f"{len(keyword_hits)} chunk(s) kept "
              f"(of {len(all_matching_keys)} raw matches)")

    # ── Vector pass ─────────────────────────────────────────────────────────
    if max_results == 0:
        if maximize_context:
            max_results = collection_total
        else:
            max_results = max(15, collection_total * 15 // 100)

    results = _collection.query(
        query_texts=[query],
        n_results=min(max_results, collection_total),
        include=["documents", "metadatas", "distances"],
    )

    vector_hits = {}  # (source_file, chunk_index) -> (document_text, distance)
    vector_count_before_filter = 0
    if results["documents"] and results["documents"][0]:
        vector_count_before_filter = len(results["documents"][0])
        filtered_dists = []
        for doc, meta, dist in zip(
            results["documents"][0], results["metadatas"][0], results["distances"][0]
        ):
            if dist <= max_distance:
                source = meta.get("source_file", "unknown")
                idx = meta.get("chunk_index", 0)
                vector_hits[(source, idx)] = (doc, dist)
                filtered_dists.append(dist)
        if filtered_dists:
            print(f"🔎 vector pass: {len(vector_hits)}/{vector_count_before_filter} chunks "
                  f"passed distance ≤ {max_distance} "
                  f"(distances: min={min(filtered_dists):.3f} max={max(filtered_dists):.3f})")
        else:
            print(f"🔎 vector pass: 0/{vector_count_before_filter} chunks "
                  f"passed distance ≤ {max_distance}")
    else:
        print(f"🔎 vector pass: no results from ChromaDB")

    # ── Merge: keyword hits first, then vector-only ─────────────────────────
    # Tag each chunk with origin and scoring metadata for priority-ordered output.
    # Tier 0 = keyword (authoritative), Tier 1 = vector-only, Tier 2 = neighbor.
    # _ORIGIN_KEYWORD = 0
    # _ORIGIN_VECTOR = 1
    # _ORIGIN_NEIGHBOR = 2
    merged = {}  # (source_file, chunk_index) -> (doc, tier, score, distance)
    for key, doc in keyword_hits.items():
        score = selective_scores.get(key, 0)
        merged[key] = (doc, 0, score, 0.0)
    for key, (doc, dist) in vector_hits.items():
        if key not in merged:
            merged[key] = (doc, 1, 0, dist)

    keyword_only_count = len(keyword_hits)
    vector_only_count = len([k for k in vector_hits if k not in keyword_hits])
    relevant_count = len(merged)

    print(f"🔎 hybrid merge: {keyword_only_count} keyword-only + "
          f"{vector_only_count} vector-only = {relevant_count} total primary chunks")

    if not merged:
        return "", 0, 0, 0, collection_total, 0

    # Collect matched chunks into per-file dicts with metadata
    file_chunks = {}  # source_file → {chunk_index: (doc, tier, score, distance)}
    for (source, idx), (doc, tier, score, dist) in merged.items():
        if source not in file_chunks:
            file_chunks[source] = {}
        file_chunks[source][idx] = (doc, tier, score, dist)

    # ── Neighbor expansion ──────────────────────────────────────────────────
    # Any primary hit can be cut mid-sentence at a chunk boundary, so neighbors
    # are considered for both keyword and vector hits. Keyword hits get priority
    # for neighbor slots since they're more likely to be mid-mention. Only pull
    # the next chunk forward (N+1) since information flows forward across
    # boundaries. Cap total neighbors at max(6, 25% of primary hits).
    _NEIGHBOR_RATIO = 0.25
    _NEIGHBOR_MIN = 6
    max_neighbors = max(_NEIGHBOR_MIN, int(relevant_count * _NEIGHBOR_RATIO))

    # Collect candidate neighbors: keyword hits first (priority), then vector-only
    neighbor_candidates = []  # list of (source, nidx) — keyword candidates first
    vector_only_hits = [k for k in vector_hits if k not in keyword_hits]

    for hit_list in [keyword_hits, vector_only_hits]:
        for (source, idx) in hit_list:
            nidx = idx + 1
            # Skip if already a primary hit or already a candidate
            if nidx in file_chunks.get(source, {}):
                continue
            candidate = (source, nidx)
            if candidate in neighbor_candidates:
                continue
            neighbor_candidates.append(candidate)

    # Trim to cap (keyword candidates are first, so they survive the cut)
    if len(neighbor_candidates) > max_neighbors:
        print(f"🔎 neighbor expansion: trimming {len(neighbor_candidates)} candidates "
              f"to cap of {max_neighbors} (primary={relevant_count}, "
              f"ratio={_NEIGHBOR_RATIO}, min={_NEIGHBOR_MIN})")
        neighbor_candidates = neighbor_candidates[:max_neighbors]

    # Fetch neighbors
    neighbors_fetched = 0
    for source, nidx in neighbor_candidates:
        nid = f"{source}::chunk{nidx}"
        try:
            fetched = _collection.get(ids=[nid], include=["documents"])
            if fetched["documents"]:
                file_chunks[source][nidx] = (fetched["documents"][0], 2, 0, 0.0)
                neighbors_fetched += 1
        except Exception:
            pass

    print(f"🔎 neighbor expansion: {neighbors_fetched} next-chunk neighbor(s) added "
          f"(cap={max_neighbors})")

    # Build result text in priority order so that when max_chars is hit,
    # the least valuable chunks are trimmed first:
    #   Tier 0 (keyword): sorted by selective score descending, then source/idx
    #   Tier 1 (vector):  sorted by distance ascending (closer = better), then source/idx
    #   Tier 2 (neighbor): sorted by source/idx
    raw_chunks = sum(len(chunks) for chunks in file_chunks.values())
    raw_chars = sum(len(v[0].encode("utf-8")) for chunks in file_chunks.values() for v in chunks.values())

    all_entries = []  # list of (tier, score, distance, source, idx, doc)
    for source, chunks in file_chunks.items():
        for idx, (doc, tier, score, dist) in chunks.items():
            all_entries.append((tier, score, dist, source, idx, doc))

    def _entry_sort_key(entry):
        tier, score, dist, source, idx, _ = entry
        if tier == 0:
            return (0, -score, source, idx)
        elif tier == 1:
            return (1, dist, source, idx)
        else:
            return (2, source, idx)

    all_entries.sort(key=_entry_sort_key)

    parts = []
    total_bytes = 0
    total_chunks_used = 0
    current_source = None
    chunk_parts = []

    for tier, score, dist, source, idx, doc in all_entries:
        chunk_bytes = len(doc.encode("utf-8"))
        if max_chars > 0 and total_bytes + chunk_bytes > max_chars:
            if chunk_parts and current_source is not None:
                parts.append(f"[{current_source}]\n" + "\n".join(chunk_parts))
                chunk_parts = []
                current_source = None
            break
        if source != current_source:
            if chunk_parts and current_source is not None:
                parts.append(f"[{current_source}]\n" + "\n".join(chunk_parts))
            chunk_parts = []
            current_source = source
        chunk_parts.append(doc)
        total_bytes += chunk_bytes
        total_chunks_used += 1

    if chunk_parts and current_source is not None:
        parts.append(f"[{current_source}]\n" + "\n".join(chunk_parts))

    if not parts:
        return "", 0, 0, 0, 0, 0

    result = (
        "--- BEGIN SEARCH CONTEXT ---\n"
        "The following search results may or may not provide additional "
        "useful context. Use where needed/appropriate.\n\n"
        + "\n\n".join(parts)
        + "\n--- END SEARCH CONTEXT ---"
    )
    return result, total_chunks_used, raw_chunks, raw_chars, collection_total, relevant_count


def is_available():
    """Return True if the vector store is initialized and ready for queries.

    Returns False while a (re)build is in progress (state.SEARCH_INITIALIZING),
    even if _collection is non-None, since the collection may be mid-write.
    """
    from . import state
    if state.SEARCH_INITIALIZING:
        return False
    return _collection is not None and _collection.count() > 0
