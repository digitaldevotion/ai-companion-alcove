# ============================================
# Alcove — database.py
# SQLite persistence layer
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================
import os
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path


# Registry to track which channel is mapped to which companion
_registry_db = None


def get_registry_db():
    global _registry_db
    if _registry_db is not None:
        return _registry_db
    Path("databases").mkdir(exist_ok=True)
    _registry_db = sqlite3.connect("databases/registry.db")
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


def _is_stub_db(path):
    # Return True if *path* is an empty/stub database — 0 bytes, or has a
    # messages table with zero rows. Used to detect a prior failed migration
    # that left an empty DB at the canonical path alongside a legacy file.
    try:
        if path.stat().st_size == 0:
            return True
    except OSError:
        return True
    conn = None
    try:
        conn = sqlite3.connect(str(path))
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM messages")
        return cursor.fetchone()[0] == 0
    except sqlite3.OperationalError:
        # No messages table — definitely a stub
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

    db = sqlite3.connect(str(open_path))
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


def save_message(db, channel, role, content, name=None):
    cursor = db.cursor()
    cursor.execute(
        "INSERT INTO messages (timestamp, channel, role, name, content) "
        "VALUES (?, ?, ?, ?, ?)",
        (datetime.now().isoformat(), channel, role, name, content)
    )
    db.commit()
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
        return {"messages": 0, "settings": 0, "anchors": 0}

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

    return {"messages": messages_count, "settings": settings_count, "anchors": anchors_count}


def prune_orphan_channels(db, live_channel_names, min_age_days=5):
    # Delete rows from messages, channel_settings, and anchored_memories for
    # any channel NOT in the provided iterable of live channel names, but only
    # for channels whose most recent activity (newest message OR newest
    # anchored memory) is older than `min_age_days`. This guards against
    # accidental deletion if the bot temporarily can't see a channel due to a
    # Discord service issue.
    #
    # The 'global' row in anchored_memories is always preserved.
    # If live_channel_names is empty, returns (0, 0, 0) without deleting.
    # Returns a tuple: (messages_removed, settings_removed, anchors_removed).
    live_list = list(live_channel_names)
    if not live_list:
        return (0, 0, 0)

    cursor = db.cursor()

    # Find the set of channels that exist in ANY of the three tables
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
        )
        WHERE channel NOT IN ({placeholders})
        """,
        live_list,
    )
    orphan_candidates = [row[0] for row in cursor.fetchall()]
    if not orphan_candidates:
        return (0, 0, 0)

    # Compute the cutoff — only channels with no activity newer than this
    # are eligible for deletion.
    cutoff_iso = (datetime.now() - timedelta(days=min_age_days)).isoformat()

    # Filter to channels whose newest activity is older than the cutoff.
    # A channel is safe to delete only if BOTH its newest message AND its
    # newest non-global anchor are older than the cutoff (or don't exist).
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
        newest = max(t for t in (newest_msg, newest_anchor) if t is not None) \
            if (newest_msg or newest_anchor) else None
        if newest is None or newest < cutoff_iso:
            safe_to_delete.append(ch)

    if not safe_to_delete:
        return (0, 0, 0)

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

    return (msg_removed, settings_removed, anchors_removed)


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
