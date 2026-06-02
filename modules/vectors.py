#!/usr/bin/env python3
# ============================================
# Alcove v1.3.0 — vectors.py
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================

import hashlib
import os
import re
from pathlib import Path

import chromadb

# ── Constants ────────────────────────────────────────────────────────────────

_COLLECTION_NAME = "search_references"
_DATA_DIR = Path(__file__).parent.parent / "search_vectors_data"
_MANIFEST_FILE = _DATA_DIR / "manifest.txt"
_CHUNK_OVERLAP = 100    # overlap chars between consecutive chunks

_client = None
_collection = None
_chunk_size = 2000  # stored from init, used to estimate results for budget-based queries


# ── Internal helpers ─────────────────────────────────────────────────────────

def _file_signature(path):
    """Return a hash of file mtime + size for change detection."""
    try:
        st = os.stat(path)
        raw = f"{path}|{st.st_mtime}|{st.st_size}"
        return hashlib.md5(raw.encode()).hexdigest()
    except OSError:
        return None


def _load_manifest():
    """Load the manifest dict (filepath -> signature) from disk."""
    manifest = {}
    if _MANIFEST_FILE.exists():
        for line in _MANIFEST_FILE.read_text().splitlines():
            if "|" in line:
                sig, fp = line.split("|", 1)
                manifest[fp] = sig
    return manifest


def _save_manifest(manifest):
    """Write the manifest dict to disk."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    lines = [f"{sig}|{fp}" for fp, sig in manifest.items()]
    _MANIFEST_FILE.write_text("\n".join(lines) + "\n")


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


# ── Public API ───────────────────────────────────────────────────────────────

def init_vector_store(reference_files, chunk_size=2000):
    """Initialize or reopen the ChromaDB vector store and sync with reference_files.

    - Embeds new or changed files
    - Removes chunks for files no longer in reference_files
    - Skips unchanged files (manifest-based change detection)
    """
    global _client, _collection, _chunk_size

    try:
        _init_vector_store_inner(reference_files, chunk_size)
    except Exception as e:
        print(f"⚠️ vector store init failed (search will be unavailable): {e}")
        _collection = None


def _init_vector_store_inner(reference_files, chunk_size):
    """Internal implementation of init_vector_store. Errors are caught by the caller."""
    global _client, _collection, _chunk_size

    _chunk_size = chunk_size
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _client = chromadb.PersistentClient(path=str(_DATA_DIR))
    _collection = _client.get_or_create_collection(
        name=_COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )

    manifest = _load_manifest()
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

    # ── Embed new or changed files ──
    embedded = 0
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

        # Remove old chunks for this file (if any) before re-embedding
        old_ids = [
            mid for mid, meta in zip(existing_meta["ids"], existing_meta["metadatas"])
            if meta.get("source_file") == filepath
        ] if existing_meta["ids"] else []
        if old_ids:
            _collection.delete(ids=old_ids)

        chunks = _chunk_text(text, chunk_size=chunk_size)
        if not chunks:
            continue

        ids = [f"{filepath}::chunk{i}" for i in range(len(chunks))]
        metas = [{"source_file": filepath, "chunk_index": i} for i in range(len(chunks))]

        _collection.add(
            ids=ids,
            documents=chunks,
            metadatas=metas,
        )
        manifest[filepath] = sig
        embedded += 1
        print(f"  📄 vectors: embedded {filepath} ({len(chunks)} chunk(s))")

    _save_manifest(manifest)

    total = _collection.count()
    print(f"  🔎 vectors: store ready — {total} chunks total "
          f"({embedded} file(s) embedded, {skipped} unchanged)")


def search_vectors(query, max_results=5, max_chars=0, maximize_context=False, max_distance=0.8, keyword_selectivity=0.10):
    """Hybrid search: keyword pass (selective exact matches) + vector pass
    (semantic neighbors). Returns (result_text, chunk_count, raw_chunks,
    raw_chars, collection_total, relevant_count), or ("", 0, 0, 0, 0, 0, 0).

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
    if _collection is None or _collection.count() == 0:
        return "", 0, 0, 0, 0, 0

    try:
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
        all_data = _collection.get(include=["documents", "metadatas"])

        # Build a lookup: (source, idx) -> document_text
        all_chunks = {}  # (source_file, chunk_index) -> document_text
        for doc, meta in zip(all_data["documents"], all_data["metadatas"]):
            source = meta.get("source_file", "unknown")
            idx = meta.get("chunk_index", 0)
            all_chunks[(source, idx)] = doc

        # Count how many chunks each keyword matches
        word_match_counts = {}
        word_patterns = {}
        for w in query_words:
            pattern = re.compile(re.escape(w), re.IGNORECASE)
            word_patterns[w] = pattern
            count = sum(1 for doc in all_data["documents"] if pattern.search(doc))
            word_match_counts[w] = count

        # Determine which keywords are selective (match < selectivity% of collection)
        max_common_hits = collection_total * keyword_selectivity
        selective_words = {w for w, c in word_match_counts.items() if c <= max_common_hits}
        common_words = {w for w, c in word_match_counts.items() if c > max_common_hits}

        if common_words:
            print(f"🔎 keyword pass: common words dropped (>{keyword_selectivity:.0%} of "
                  f"{collection_total}): "
                  + ", ".join(f"{w!r} ({word_match_counts[w]})" for w in common_words))

        # Build per-word chunk sets
        word_chunk_sets = {}  # word -> set of (source, idx)
        for key, doc in all_chunks.items():
            for w in query_words:
                if word_patterns[w].search(doc):
                    if w not in word_chunk_sets:
                        word_chunk_sets[w] = set()
                    word_chunk_sets[w].add(key)

        # Keep chunks that match at least one selective keyword.
        # Chunks that ONLY match common keywords are dropped.
        kept_keys = set()
        for w in selective_words:
            if w in word_chunk_sets:
                kept_keys.update(word_chunk_sets[w])

        # Also keep chunks that match BOTH a common keyword AND a selective one
        # (they're already in kept_keys from the selective word above).
        # Chunks matching ONLY common words are excluded.

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

    vector_hits = {}  # (source_file, chunk_index) -> document_text
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
                vector_hits[(source, idx)] = doc
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
    # Use ordered dict to preserve insertion order: keyword matches are
    # authoritative and come first; vector-only matches follow.
    merged = {}  # (source_file, chunk_index) -> document_text
    for key, doc in keyword_hits.items():
        merged[key] = doc
    for key, doc in vector_hits.items():
        if key not in merged:
            merged[key] = doc

    keyword_only_count = len(keyword_hits)
    vector_only_count = len([k for k in vector_hits if k not in keyword_hits])
    relevant_count = len(merged)

    print(f"🔎 hybrid merge: {keyword_only_count} keyword-only + "
          f"{vector_only_count} vector-only = {relevant_count} total primary chunks")

    if not merged:
        return "", 0, 0, 0, collection_total, 0

    # Collect matched chunks into per-file dicts
    file_chunks = {}  # source_file → {chunk_index: document_text}
    for (source, idx), doc in merged.items():
        if source not in file_chunks:
            file_chunks[source] = {}
        file_chunks[source][idx] = doc

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
                file_chunks[source][nidx] = fetched["documents"][0]
                neighbors_fetched += 1
        except Exception:
            pass

    print(f"🔎 neighbor expansion: {neighbors_fetched} next-chunk neighbor(s) added "
          f"(cap={max_neighbors})")

    # Build result text, chunk by chunk, enforcing max_chars per chunk
    # rather than per file so a single large file can't blow the budget.
    raw_chunks = sum(len(chunks) for chunks in file_chunks.values())
    raw_chars = sum(len(chunks[idx]) for chunks in file_chunks.values() for idx in chunks)
    parts = []
    total_chars = 0
    total_chunks_used = 0
    for source in sorted(file_chunks):
        chunks = file_chunks[source]
        chunk_parts = []
        for idx in sorted(chunks):
            chunk_text = chunks[idx]
            if max_chars > 0 and total_chars + len(chunk_text) > max_chars:
                break
            chunk_parts.append(chunk_text)
            total_chars += len(chunk_text)
            total_chunks_used += 1
        if chunk_parts:
            parts.append(f"[{source}]\n" + "\n".join(chunk_parts))
        if max_chars > 0 and total_chars >= max_chars:
            break

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
    """Return True if the vector store is initialized and ready for queries."""
    return _collection is not None and _collection.count() > 0
