# ============================================
# Alcove — database.py
# SQLite persistence layer
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================
import os
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


# Registry to track which channel is mapped to which companion
_registry_db = None


def get_registry_db():
    global _registry_db
    if _registry_db is not None:
        return _registry_db
    Path("databases").mkdir(exist_ok=True)
    # check_same_thread=False: this connection is created in the main thread
    # but the boot vector-init daemon thread (and asyncio.to_thread callers)
    # also read from it via get_channel_companion. SQLite's C library is
    # thread-safe in serialized mode; the flag just removes Python's
    # thread-affinity guard. timeout=5.0 sets a busy timeout so concurrent
    # access waits instead of raising SQLITE_BUSY.
    _registry_db = sqlite3.connect("databases/registry.db", check_same_thread=False, timeout=5.0)
    try:
        _registry_db.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        pass
    _registry_db.execute("""
        CREATE TABLE IF NOT EXISTS channel_registry (
            channel TEXT PRIMARY KEY,
            companion TEXT NOT NULL
        )
    """)
    _registry_db.execute("""
        CREATE TABLE IF NOT EXISTS registry_channel_settings (
            channel TEXT NOT NULL,
            param   TEXT NOT NULL,
            value   TEXT,
            PRIMARY KEY (channel, param)
        )
    """)
    _registry_db.execute("""
        CREATE TABLE IF NOT EXISTS registry_global_vars (
            param     TEXT PRIMARY KEY,
            value     TEXT,
            timestamp TEXT
        )
    """)
    _registry_db.commit()
    return _registry_db


def get_channel_companion(channel_name):
    try:
        conn = get_registry_db()
        cursor = conn.cursor()
        cursor.execute("SELECT companion FROM channel_registry WHERE channel = ?", (channel_name,))
        row = cursor.fetchone()
        return row[0] if row else "default"
    except Exception as e:
        print(f"⚠️ Error reading companion registry: {e}")
        return "default"


def set_channel_companion(channel_name, companion_name):
    try:
        conn = get_registry_db()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO channel_registry (channel, companion)
            VALUES (?, ?)
            ON CONFLICT(channel) DO UPDATE SET companion = excluded.companion
        """, (channel_name, companion_name))
        conn.commit()
    except Exception as e:
        print(f"⚠️ Error updating companion registry: {e}")


# Cached connections: companion_name -> sqlite3.Connection
_db_connections = {}

# Tables a live companion DB is expected to have. _is_stub_db requires ALL of
# them to be missing or empty before judging the file a disposable stub —
# judging by `messages` alone destroyed anchors/settings/crontab when history
# had been cleared (e.g. via !clear) while a legacy file existed alongside.
_COMPANION_TABLES = ("messages", "anchored_memories", "channel_settings",
                     "global_vars", "crontab")


def _is_stub_db(path):
    # Return True only if *path* is a genuinely empty/stub database — 0 bytes,
    # unreadable, or every known table absent/zero-row. Used to detect a prior
    # failed migration that left an empty DB at the canonical path alongside a
    # legacy file. Any real data in ANY table means "not a stub".
    try:
        if path.stat().st_size == 0:
            return True
    except OSError:
        return True
    conn = None
    try:
        conn = sqlite3.connect(str(path), check_same_thread=False, timeout=5.0)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        existing_tables = {row[0] for row in cursor.fetchall()}
        for table in _COMPANION_TABLES:
            if table not in existing_tables:
                continue  # absent table contributes "empty"
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            if cursor.fetchone()[0] > 0:
                return False
        return True
    except sqlite3.DatabaseError:
        # Unreadable/corrupt/non-SQLite file (or genuinely no tables at all)
        return True
    finally:
        if conn is not None:
            conn.close()


def _migrate_legacy_db(old_db_path, db_path):
    # Move the legacy companion_data.db into the default companion directory.
    # Tries os.rename first, falls back to copy+unlink for cross-filesystem
    # moves. Returns True on success, False on failure (legacy file is left
    # in place so the caller can fall back to opening it directly).
    print("🚚 Moving legacy SQLite history database into default compartment folder...")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(old_db_path, db_path)
        print("✅ Database migration completed successfully.")
        return True
    except OSError:
        pass
    try:
        shutil.copy2(old_db_path, db_path)
        os.unlink(old_db_path)
        print("✅ Database migration completed (via copy).")
        return True
    except Exception as e:
        print(f"❌ Database file migration failed: {e}")
        print(f"   Legacy file remains at: {old_db_path}")
        return False


def init_database(companion_name="default"):
    databases_dir = Path("databases")
    databases_dir.mkdir(exist_ok=True)

    companion_dir = databases_dir / companion_name
    companion_dir.mkdir(parents=True, exist_ok=True)

    db_path = companion_dir / "companion_data.db"

    # --- Seamless Migration ---
    # If a legacy databases/companion_data.db exists, move it into
    # databases/default/companion_data.db. Handles three scenarios:
    #   1. Fresh migration (db_path doesn't exist yet)
    #   2. Retry after prior failed migration (db_path is an empty stub)
    #   3. Both exist with real data (warn, don't touch either)
    open_path = db_path
    if companion_name == "default":
        old_db_path = databases_dir / "companion_data.db"
        if old_db_path.exists():
            if not db_path.exists() or _is_stub_db(db_path):
                if db_path.exists():
                    try:
                        db_path.unlink()
                    except Exception:
                        pass
                if not _migrate_legacy_db(old_db_path, db_path):
                    open_path = old_db_path
            else:
                print(f"⚠️ Legacy database found at {old_db_path}")
                print(f"   alongside existing {db_path} (which has data).")
                print(f"   The legacy file was NOT migrated. If your history is missing,")
                print(f"   stop the bot and move the legacy file into place manually.")

    db = sqlite3.connect(str(open_path), check_same_thread=False, timeout=5.0)
    # WAL lets readers and writers proceed concurrently instead of blocking on
    # the busy timeout when cron/idle/background threads touch the same DB as
    # the event loop. Idempotent; journal_mode persists per database file.
    try:
        db.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError as _wal_err:
        print(f"⚠️ Could not enable WAL mode on {open_path}: {_wal_err}")
    cursor = db.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            channel TEXT NOT NULL,
            role TEXT NOT NULL,
            name TEXT,
            content TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS anchored_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            channel TEXT NOT NULL DEFAULT 'global',
            content TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS channel_settings (
            channel TEXT NOT NULL,
            param TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (channel, param)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS global_vars (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            param TEXT NOT NULL UNIQUE,
            value TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS crontab (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type  TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'active',
            "when"      TEXT NOT NULL,
            prompt      TEXT NOT NULL,
            channel     TEXT NOT NULL,
            last_fired  TEXT,
            created_at  TEXT NOT NULL,
            fail_count  INTEGER NOT NULL DEFAULT 0
        )
    """)
    # Migration: add fail_count column to existing crontab tables.
    cursor.execute("PRAGMA table_info(crontab)")
    existing_cols = {row[1] for row in cursor.fetchall()}
    if "fail_count" not in existing_cols:
        cursor.execute("ALTER TABLE crontab ADD COLUMN fail_count INTEGER NOT NULL DEFAULT 0")
        print("📦 Migrated crontab table: added fail_count column")
    db.commit()
    return db


def get_db(companion_name="default"):
    """
    Returns the sqlite connection context for a specific companion.
    Normalizes connections dynamically on-the-fly.
    """
    global _db_connections
    if not companion_name:
        companion_name = "default"

    # Normalize name
    normalized = companion_name.strip().lower()
    if normalized not in _db_connections:
        _db_connections[normalized] = init_database(companion_name)
    return _db_connections[normalized]


# Throttle state for touch_channel: (id(conn), channel) -> last-write time
# (time.monotonic). The staleness granularity that matters is days, so one
# write per TOUCH_THROTTLE_SECONDS per channel is plenty and keeps the
# heartbeat off the hot path for busy channels.
_touch_throttle = {}
TOUCH_THROTTLE_SECONDS = 15 * 60

LAST_TOUCHED_PARAM = "last_touched"


def touch_channel(db, channel):
    # Record recent activity for a channel key so prune_orphan_channels can
    # distinguish "quiet" from "dead" — commands (!help etc.) are never saved
    # to history, so without this a command-only channel looks maximally
    # stale. Throttled in-process; never raises so it is safe on hot paths
    # (on_message, save_message). Timestamp convention matches save_message:
    # naive host-local ISO, which is what the prune cutoff compares against.
    try:
        now_mono = time.monotonic()
        throttle_key = (id(db), channel)
        last = _touch_throttle.get(throttle_key)
        if last is not None and (now_mono - last) < TOUCH_THROTTLE_SECONDS:
            return False
        cursor = db.cursor()
        cursor.execute(
            "INSERT INTO channel_settings (channel, param, value) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(channel, param) DO UPDATE SET value = excluded.value",
            (channel, LAST_TOUCHED_PARAM, datetime.now().isoformat()),
        )
        db.commit()
        _touch_throttle[throttle_key] = now_mono
        return True
    except Exception as e:
        print(f"⚠️ touch_channel failed for {channel}: {e}")
        return False


def save_message(db, channel, role, content, name=None):
    cursor = db.cursor()
    cursor.execute(
        "INSERT INTO messages (timestamp, channel, role, name, content) "
        "VALUES (?, ?, ?, ?, ?)",
        (datetime.now().isoformat(), channel, role, name, content)
    )
    db.commit()
    # A persisted message IS activity: piggyback the prune heartbeat here so
    # every save path (text turns, live-voice transcripts, system markers)
    # keeps its channel alive without each caller having to remember to.
    touch_channel(db, channel)
    # Return the new row's id so callers can scope subsequent history reads
    # to messages that existed *before* this one (see get_recent_messages'
    # before_id param). This prevents the just-saved turn from appearing
    # twice in an LLM payload that also appends it explicitly.
    return cursor.lastrowid


def get_recent_messages(db, channel, before_id=None):
    # When `before_id` is provided, only rows with id < before_id are
    # returned. This lets a caller that has just saved the current turn
    # load the history that existed prior to that save, avoiding a
    # duplicate when it then appends the current turn itself. Messages
    # saved concurrently by other handlers (with higher ids) are also
    # excluded, so each turn sees the conversation strictly as-of when
    # it arrived — no seeing-the-future, no gaps, no misordering.
    #
    # DESIGN NOTE — intentional no LIMIT / full-channel SELECT:
    # The query deliberately returns every row for the channel and lets
    # trimming happen at prompt-assembly time (see build_trimmed_history_
    # for_payload in llm_prompt_builder.py), NOT at the SQL layer. This is an
    # intentional memory-vs-disk tradeoff: SQLite serves the full channel
    # result set very quickly, and holding only the trimmed slice in RAM
    # (rather than caching full message lists in-process) keeps Alcove's
    # footprint small enough to run on low-memory hosts (Raspberry Pi,
    # small EC2 instances, etc.). Re-tokenization of the loaded rows is
    # absorbed by provider prompt caching, so the per-turn cost remains
    # low. Do NOT "fix" this by adding a LIMIT — the trim floor/ceiling
    # logic in llm_prompt_builder.py depends on seeing the full channel history.
    cursor = db.cursor()
    if before_id is None:
        cursor.execute(
            "SELECT role, name, content FROM messages "
            "WHERE channel = ? ORDER BY id",
            (channel,)
        )
    else:
        cursor.execute(
            "SELECT role, name, content FROM messages "
            "WHERE channel = ? AND id < ? ORDER BY id",
            (channel, before_id)
        )
    rows = cursor.fetchall()
    messages = []
    for role, name, content in rows:
        if role == "user":
            messages.append({
                "role": "user",
                "content": f"{name}: {content}"
            })
        else:
            messages.append({
                "role": "assistant",
                "content": f"{content}"
            })
    return messages


def get_registry_channel_setting(channel, param, default=None):
    # Cross-companion per-channel setting stored in registry.db.
    conn = get_registry_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT value FROM registry_channel_settings WHERE channel = ? AND param = ?",
        (channel, param)
    )
    row = cursor.fetchone()
    return row[0] if row else default


def set_registry_channel_setting(channel, param, value):
    conn = get_registry_db()
    conn.execute(
        "INSERT INTO registry_channel_settings (channel, param, value) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(channel, param) DO UPDATE SET value = excluded.value",
        (channel, param, value)
    )
    conn.commit()


def clear_registry_channel_setting(channel, param):
    conn = get_registry_db()
    conn.execute(
        "DELETE FROM registry_channel_settings WHERE channel = ? AND param = ?",
        (channel, param)
    )
    conn.commit()
    return conn.total_changes > 0


def list_registry_channel_settings_by_prefix(channel, prefix):
    conn = get_registry_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT param, value FROM registry_channel_settings "
        "WHERE channel = ? AND param LIKE ? ORDER BY param",
        (channel, f"{prefix}%"),
    )
    return cursor.fetchall()


def get_registry_global_var(param):
    conn = get_registry_db()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM registry_global_vars WHERE param = ?", (param,))
    row = cursor.fetchone()
    return row[0] if row else None


def set_registry_global_var(param, value):
    conn = get_registry_db()
    conn.execute(
        "INSERT INTO registry_global_vars (timestamp, param, value) VALUES (?, ?, ?) "
        "ON CONFLICT(param) DO UPDATE SET "
        "value = excluded.value, timestamp = excluded.timestamp",
        (datetime.now().isoformat(), param, value)
    )
    conn.commit()


def delete_registry_global_var(param):
    conn = get_registry_db()
    conn.execute("DELETE FROM registry_global_vars WHERE param = ?", (param,))
    conn.commit()
    return conn.total_changes > 0


def list_registry_global_vars(param_prefix=None):
    conn = get_registry_db()
    cursor = conn.cursor()
    if param_prefix:
        cursor.execute(
            "SELECT param, value FROM registry_global_vars WHERE param LIKE ? ORDER BY param",
            (f"{param_prefix}%",)
        )
    else:
        cursor.execute("SELECT param, value FROM registry_global_vars ORDER BY param")
    return cursor.fetchall()


# --- social_mode (cross-companion; stored in registry.db) ---

def is_social_mode(channel_name):
    return get_registry_channel_setting(channel_name, "social_mode", "0") == "1"


def set_social_mode(channel_name):
    set_registry_channel_setting(channel_name, "social_mode", "1")


def clear_social_mode(channel_name):
    clear_registry_channel_setting(channel_name, "social_mode")


def get_social_mode_focus(guild_name):
    return get_registry_global_var(f"social_mode_focus::{guild_name}")


def set_social_mode_focus(guild_name, channel_name):
    set_registry_global_var(f"social_mode_focus::{guild_name}", channel_name)


def clear_social_mode_focus(guild_name):
    delete_registry_global_var(f"social_mode_focus::{guild_name}")


def get_anchored_memories(db, channel_name):
    # Return anchored memories as (id, content) tuples for the prompt:
    # globals first, then channel-specific. The ID is included so the
    # prompt can render real anchor IDs — those are what tools like
    # deleteGlobalAnchor reference.
    cursor = db.cursor()
    cursor.execute(
        "SELECT id, content FROM anchored_memories "
        "WHERE channel = 'global' ORDER BY id"
    )
    global_memories = [(row[0], row[1]) for row in cursor.fetchall()]
    cursor.execute(
        "SELECT id, content FROM anchored_memories "
        "WHERE channel = ? ORDER BY id",
        (channel_name,)
    )
    channel_memories = [(row[0], row[1]) for row in cursor.fetchall()]
    return global_memories + channel_memories


def get_anchored_memory(db, memory_id):
    # Return the content of a single anchored memory by id, or None if no
    # such row exists. Used for surfacing the old/new text of a memory when
    # manageAnchor adds, updates, or removes it.
    cursor = db.cursor()
    cursor.execute("SELECT content FROM anchored_memories WHERE id = ?", (memory_id,))
    row = cursor.fetchone()
    return row[0] if row else None


def add_anchored_memory(db, channel, content):
    cursor = db.cursor()
    cursor.execute(
        "INSERT INTO anchored_memories (timestamp, channel, content) VALUES (?, ?, ?)",
        (datetime.now().isoformat(), channel, content)
    )
    db.commit()


def remove_anchored_memory(db, memory_id):
    cursor = db.cursor()
    cursor.execute("DELETE FROM anchored_memories WHERE id = ?", (memory_id,))
    db.commit()
    return cursor.rowcount > 0


def update_anchored_memory(db, memory_id, content):
    # Replace an existing anchored memory's content and refresh its timestamp.
    # Returns True if a row with that id existed and was updated; False otherwise.
    cursor = db.cursor()
    cursor.execute(
        "UPDATE anchored_memories SET content = ?, timestamp = ? WHERE id = ?",
        (content, datetime.now().isoformat(), memory_id)
    )
    db.commit()
    return cursor.rowcount > 0


def list_anchored_memories(db, channel_name):
    # List memories for display: globals first, then channel-specific.
    cursor = db.cursor()
    cursor.execute(
        "SELECT id, channel, content FROM anchored_memories "
        "WHERE channel IN ('global', ?) ORDER BY "
        "CASE WHEN channel = 'global' THEN 0 ELSE 1 END, id",
        (channel_name,)
    )
    return cursor.fetchall()


def rename_channel(db, old_name, new_name):
    cursor = db.cursor()

    cursor.execute(
        "SELECT COUNT(*) FROM messages WHERE channel = ?",
        (old_name,)
    )
    if cursor.fetchone()[0] == 0:
        return {"messages": 0, "settings": 0, "anchors": 0, "crontab": 0}

    cursor.execute(
        "DELETE FROM channel_settings WHERE channel = ?",
        (new_name,)
    )
    cursor.execute(
        "DELETE FROM anchored_memories WHERE channel = ? AND channel != 'global'",
        (new_name,)
    )

    cursor.execute(
        "UPDATE channel_settings SET channel = ? WHERE channel = ?",
        (new_name, old_name)
    )
    settings_count = cursor.rowcount

    cursor.execute(
        "UPDATE anchored_memories SET channel = ? WHERE channel = ? AND channel != 'global'",
        (new_name, old_name)
    )
    anchors_count = cursor.rowcount

    cursor.execute(
        "UPDATE messages SET channel = ? WHERE channel = ?",
        (new_name, old_name)
    )
    messages_count = cursor.rowcount

    # Scheduled tasks must follow their channel, or they keep firing prompts
    # addressed to the old (now nonexistent) channel name.
    cursor.execute(
        'UPDATE crontab SET channel = ? WHERE channel = ?',
        (new_name, old_name)
    )
    crontab_count = cursor.rowcount

    db.commit()

    try:
        conn = get_registry_db()
        conn.execute(
            "UPDATE channel_registry SET channel = ? WHERE channel = ?",
            (new_name, old_name)
        )
        conn.commit()
    except Exception:
        pass

    return {"messages": messages_count, "settings": settings_count,
            "anchors": anchors_count, "crontab": crontab_count}


def prune_orphan_channels(db, live_channel_names, min_age_days=None):
    # Delete rows from messages, channel_settings, anchored_memories, and
    # crontab for any channel NOT in the provided iterable of live channel
    # names, but only when the key looks genuinely dead. Activity is judged
    # in two branches:
    #   • Keys WITH saved conversation (messages or anchored memories):
    #     eligible only when the newest message/anchor is older than
    #     `min_age_days` AND the `last_touched` heartbeat is missing or
    #     equally stale — a fresh heartbeat vetoes the purge, since touches
    #     only come from real Discord traffic and prove the key is still
    #     alive even if every saved message predates the cutoff.
    #   • Keys with NO conversation at all (e.g. command-only channels —
    #     commands are never written to history): eligible only when their
    #     `last_touched` heartbeat is missing or older than `min_age_days`.
    #     The heartbeat is refreshed on every on_message event and every
    #     save_message call (see touch_channel), so a live channel that
    #     merely hasn't chatted stays safe.
    # This guards against accidental deletion if the bot temporarily can't
    # see a channel due to a Discord service issue.
    #
    # The 'global' row in anchored_memories is always preserved.
    # If live_channel_names is empty, returns (0, 0, 0, 0) without deleting.
    # Returns a tuple: (messages_removed, settings_removed, anchors_removed,
    # crontab_removed).
    live_list = list(live_channel_names)
    if not live_list:
        return (0, 0, 0, 0)

    if min_age_days is None:
        try:
            import config as _config
            min_age_days = getattr(_config, "PRUNE_MIN_AGE_DAYS", 180)
        except Exception:
            min_age_days = 180

    cursor = db.cursor()

    # Find the set of channels that exist in ANY of the four tables
    # but are not in the live list.
    placeholders = ",".join("?" * len(live_list))
    cursor.execute(
        f"""
        SELECT DISTINCT channel FROM (
            SELECT channel FROM messages
            UNION
            SELECT channel FROM channel_settings
            UNION
            SELECT channel FROM anchored_memories WHERE channel != 'global'
            UNION
            SELECT channel FROM crontab
        )
        WHERE channel NOT IN ({placeholders})
        """,
        live_list,
    )
    orphan_candidates = [row[0] for row in cursor.fetchall()]
    if not orphan_candidates:
        return (0, 0, 0, 0)

    # Compute the cutoff — only channels with no activity newer than this
    # are eligible for deletion.
    cutoff_iso = (datetime.now() - timedelta(days=min_age_days)).isoformat()

    # Filter to dead keys. A fresh last_touched heartbeat vetoes the purge
    # in BOTH branches — touches only originate from real traffic (on_message
    # / save_message), so recent activity always proves a key is alive. For
    # zero-conversation keys the heartbeat is the ONLY signal, and a missing
    # one does NOT rescue the key: touch_channel runs before the prune task's
    # first fire after boot, so any key still untouched by then was untouched
    # in reality too.
    safe_to_delete = []
    for ch in orphan_candidates:
        cursor.execute(
            "SELECT MAX(timestamp) FROM messages WHERE channel = ?",
            (ch,),
        )
        newest_msg = cursor.fetchone()[0]
        cursor.execute(
            "SELECT MAX(timestamp) FROM anchored_memories WHERE channel = ?",
            (ch,),
        )
        newest_anchor = cursor.fetchone()[0]
        cursor.execute(
            "SELECT value FROM channel_settings WHERE channel = ? AND param = ?",
            (ch, LAST_TOUCHED_PARAM),
        )
        row = cursor.fetchone()
        last_touched = row[0] if row else None

        if newest_msg is not None or newest_anchor is not None:
            newest = max(t for t in (newest_msg, newest_anchor) if t is not None)
            history_stale = newest < cutoff_iso
            touched_recently = last_touched is not None and last_touched >= cutoff_iso
            if history_stale and not touched_recently:
                safe_to_delete.append(ch)
        else:
            if last_touched is None or last_touched < cutoff_iso:
                safe_to_delete.append(ch)

    if not safe_to_delete:
        return (0, 0, 0, 0)

    del_placeholders = ",".join("?" * len(safe_to_delete))

    with db:
        cursor.execute(
            f"DELETE FROM messages WHERE channel IN ({del_placeholders})",
            safe_to_delete,
        )
        msg_removed = cursor.rowcount

        cursor.execute(
            f"DELETE FROM channel_settings WHERE channel IN ({del_placeholders})",
            safe_to_delete,
        )
        settings_removed = cursor.rowcount

        cursor.execute(
            f"DELETE FROM anchored_memories WHERE channel != 'global' AND channel IN ({del_placeholders})",
            safe_to_delete,
        )
        anchors_removed = cursor.rowcount

        # Tasks for deleted channels would fire forever into nonexistent
        # channels, racking up fail_count with no way to recover.
        cursor.execute(
            f"DELETE FROM crontab WHERE channel IN ({del_placeholders})",
            safe_to_delete,
        )
        crontab_removed = cursor.rowcount

    print(f"🧹 prune_orphan_channels: purged {len(safe_to_delete)} orphaned "
          f"channel key(s): {safe_to_delete}")

    return (msg_removed, settings_removed, anchors_removed, crontab_removed)


def get_message_count(db):
    cursor = db.cursor()
    cursor.execute("SELECT COUNT(*) FROM messages")
    return cursor.fetchone()[0]


def get_channel_setting(db, channel, param, default=None):
    # Get a per-channel setting. Returns default if not set.
    cursor = db.cursor()
    cursor.execute(
        "SELECT value FROM channel_settings WHERE channel = ? AND param = ?",
        (channel, param)
    )
    row = cursor.fetchone()
    return row[0] if row else default


def set_channel_setting(db, channel, param, value):
    # Set a per-channel setting (upsert).
    cursor = db.cursor()
    cursor.execute(
        "INSERT INTO channel_settings (channel, param, value) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(channel, param) DO UPDATE SET value = excluded.value",
        (channel, param, value)
    )
    db.commit()


def clear_channel_setting(db, channel, param):
    # Remove a per-channel setting (revert to default).
    cursor = db.cursor()
    cursor.execute(
        "DELETE FROM channel_settings WHERE channel = ? AND param = ?",
        (channel, param)
    )
    db.commit()
    return cursor.rowcount > 0


def get_global_var(db, param):
    # Get a global variable by param name. Returns None if not set.
    cursor = db.cursor()
    cursor.execute("SELECT value FROM global_vars WHERE param = ?", (param,))
    row = cursor.fetchone()
    return row[0] if row else None


def set_global_var(db, param, value):
    # Upsert a global variable by param name.
    cursor = db.cursor()
    cursor.execute(
        "INSERT INTO global_vars (timestamp, param, value) VALUES (?, ?, ?) "
        "ON CONFLICT(param) DO UPDATE SET "
        "value = excluded.value, timestamp = excluded.timestamp",
        (datetime.now().isoformat(), param, value)
    )
    db.commit()


def delete_global_var(db, param):
    # Remove a global variable by param name. Returns True if deleted.
    cursor = db.cursor()
    cursor.execute("DELETE FROM global_vars WHERE param = ?", (param,))
    db.commit()
    return cursor.rowcount > 0


def list_global_vars(db, param_prefix=None):
    # List global variables. If param_prefix is given, only matching params.
    cursor = db.cursor()
    if param_prefix:
        cursor.execute(
            "SELECT param, value FROM global_vars WHERE param LIKE ? ORDER BY param",
            (f"{param_prefix}%",)
        )
    else:
        cursor.execute("SELECT param, value FROM global_vars ORDER BY param")
    return cursor.fetchall()


def list_channel_settings_by_prefix(db, channel, prefix):
    # List (param, value) pairs for a channel whose param starts with the prefix.
    cursor = db.cursor()
    cursor.execute(
        "SELECT param, value FROM channel_settings "
        "WHERE channel = ? AND param LIKE ? ORDER BY param",
        (channel, f"{prefix}%"),
    )
    return cursor.fetchall()


def reset_channel_settings(db, channel):
    cursor = db.cursor()
    cursor.execute("DELETE FROM channel_settings WHERE channel = ?", (channel,))
    db.commit()
    return cursor.rowcount


def reset_all_channel_settings(db, param=None):
    # Clear settings across all channels.
    # If param is given, only clear that param. Otherwise clear everything.
    # Returns number of rows deleted.
    cursor = db.cursor()
    if param:
        cursor.execute("DELETE FROM channel_settings WHERE param = ?", (param,))
    else:
        cursor.execute("DELETE FROM channel_settings")
    db.commit()
    return cursor.rowcount


# ============================================
# CRONTAB — scheduled prompt entries
# Each companion DB has its own crontab table.
#   event_type: 'recurring' (when = 5-field cron string)
#               'once'      (when = local-naive ISO datetime)
#   status:     'active' (default) | 'disabled'
#   last_fired: ISO timestamp of last execution (recurring catch-up, once retry)
# ============================================

def _bot_local_now_iso():
    # Bot-local naive ISO timestamp using the same convention as
    # cron._local_now_naive: local = UTC + config.TIMEZONE_OFFSET. Seeding
    # last_fired/created_at with the host wall clock instead made recurring
    # jobs skip slots (or fire immediately) whenever host TZ != configured
    # offset — including every DST season on fixed-offset hosts.
    try:
        import config as _config
        _offset_hours = getattr(_config, "TIMEZONE_OFFSET", 0)
    except Exception:
        _offset_hours = 0
    now_local = datetime.now(tz=timezone.utc) + timedelta(hours=_offset_hours)
    return now_local.replace(tzinfo=None).isoformat()


def add_crontab_entry(db, event_type, when, prompt, channel, status="active"):
    now_ts = _bot_local_now_iso()
    # For recurring jobs, seed last_fired with the creation timestamp so the
    # cron loop's "newer than last_fired" gate doesn't treat the most recent
    # already-elapsed cron slot as due on the first tick. Once jobs keep
    # last_fired NULL so _check_once still fires when their time arrives.
    last_fired = now_ts if event_type == "recurring" else None
    cursor = db.cursor()
    cursor.execute(
        'INSERT INTO crontab (event_type, status, "when", prompt, channel, created_at, last_fired) '
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (event_type, status, when, prompt, channel, now_ts, last_fired),
    )
    db.commit()
    return cursor.lastrowid


def update_crontab_entry(db, entry_id, **fields):
    allowed = {"event_type", "status", "when", "prompt", "channel", "fail_count", "last_fired"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    assignments = ", ".join(f'"{k}" = ?' for k in updates.keys())
    params = list(updates.values()) + [entry_id]
    cursor = db.cursor()
    cursor.execute(
        f'UPDATE crontab SET {assignments} WHERE id = ?',
        params,
    )
    db.commit()
    return cursor.rowcount > 0


def delete_crontab_entry(db, entry_id):
    cursor = db.cursor()
    cursor.execute("DELETE FROM crontab WHERE id = ?", (entry_id,))
    db.commit()
    return cursor.rowcount > 0


def get_crontab_entry(db, entry_id):
    cursor = db.cursor()
    cursor.execute("SELECT * FROM crontab WHERE id = ?", (entry_id,))
    row = cursor.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cursor.description]
    return dict(zip(cols, row))


def list_crontab_entries(db, status="active"):
    cursor = db.cursor()
    if status is None:
        cursor.execute("SELECT * FROM crontab ORDER BY id")
    else:
        cursor.execute("SELECT * FROM crontab WHERE status = ? ORDER BY id", (status,))
    rows = cursor.fetchall()
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, r)) for r in rows]


def set_crontab_last_fired(db, entry_id, iso_ts):
    cursor = db.cursor()
    cursor.execute(
        "UPDATE crontab SET last_fired = ? WHERE id = ?",
        (iso_ts, entry_id),
    )
    db.commit()
    return cursor.rowcount > 0


def increment_crontab_fail_count(db, entry_id):
    """Increment fail_count for a crontab entry. Returns the new count."""
    cursor = db.cursor()
    cursor.execute(
        'UPDATE crontab SET fail_count = fail_count + 1 WHERE id = ?',
        (entry_id,),
    )
    db.commit()
    cursor.execute('SELECT fail_count FROM crontab WHERE id = ?', (entry_id,))
    row = cursor.fetchone()
    return row[0] if row else 0


def reset_crontab_fail_count(db, entry_id):
    """Reset fail_count to 0 after a successful fire or when re-enabling a task."""
    cursor = db.cursor()
    cursor.execute(
        'UPDATE crontab SET fail_count = 0 WHERE id = ?',
        (entry_id,),
    )
    db.commit()
    return cursor.rowcount > 0
