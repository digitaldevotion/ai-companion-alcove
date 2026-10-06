# ============================================
# Alcove — voice_live_viz.py
# Video image snapshot injection for live voice mode.
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt
# file for more information.
# ============================================
#
# This module assembles ephemeral text descriptions of video image
# snapshots (produced elsewhere by a vision model analyzing the Discord
# video portion of a live voice chat) into a single prompt block that
# is injected into the LLM context when live voice mode is active.
#
# The snapshot files themselves are NOT created here — they are plain
# text files dropped into input/viz/ by an external process. This
# module only reads the most recent ones (by mtime) and formats them
# into a block string. The returned block is appended to full_messages
# by main.py as an ephemeral user message and is never persisted to
# the messages table.
#
# Each file's contents are flattened to a single line (carriage
# returns and newlines are collapsed to spaces) so that within the
# assembled block a new line always represents the next timestamp +
# file entry.
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

# Project root (parent of modules/).
_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_VIZ_DIR = _ROOT / "input" / "viz"

# Block wrapper text. The "may or may not" hedge mirrors the way
# inject_search_context signals that injected context might not be
# relevant — keeps the model from over-indexing on noisy snapshots.
_HEADER = "--- BEGIN VIDEO IMAGE SNAPSHOTS ---"
_SUBTITLE = ("The following image snapshot metadata may or may not "
             "provide additional useful context for you:")
_FOOTER = "--- END VIDEO IMAGE SNAPSHOTS ---"


def build_snapshot_block(max_snapshots, viz_dir=None):
    """Assemble the most recent video image snapshots into a prompt block.

    Scans `viz_dir` (default: <root>/input/viz/) for *.txt files, sorts
    them by modification time (newest first), and takes the top
    `max_snapshots`. Each file's contents are flattened to a single
    line (\\r and \\n collapsed to spaces) and prefixed with the file's
    mtime as `YYYY-MM-DD HH:MM:SS - <content>`.

    Returns the assembled block string, or None if the directory is
    missing, empty, contains no .txt files, or max_snapshots <= 0.
    """
    if max_snapshots is None or max_snapshots <= 0:
        return None

    viz_path = Path(viz_dir) if viz_dir is not None else _DEFAULT_VIZ_DIR
    if not viz_path.is_dir():
        return None

    # Only top-level *.txt files (no recursion, no non-txt). Sorted by
    # mtime descending so the most recent snapshots win.
    txt_files = [p for p in viz_path.iterdir()
                 if p.is_file() and p.suffix.lower() == ".txt"]
    if not txt_files:
        return None

    txt_files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    selected = txt_files[:max_snapshots]

    # Local timezone offset, matching main.py's datetime handling.
    tz = timezone(timedelta(hours=config.TIMEZONE_OFFSET))

    lines = []
    for p in selected:
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            # Skip unreadable files rather than poisoning the block.
            continue
        # Flatten to a single line: collapse CR and LF to spaces, then
        # squeeze runs of whitespace so the block stays one line per file.
        flat = raw.replace("\r", " ").replace("\n", " ")
        flat = " ".join(flat.split()).strip()
        if not flat:
            continue
        mtime = os.path.getmtime(p)
        ts = datetime.fromtimestamp(mtime, tz=tz).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"{ts} - {flat}")

    if not lines:
        return None

    # Blank line between entries, matching the spec example.
    body = "\n\n".join(lines)
    return f"{_HEADER}\n{_SUBTITLE}\n\n{body}\n\n{_FOOTER}"
