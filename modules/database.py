# ============================================
# Alcove v1.3.0 — database.py
# SQLite persistence layer
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================
import sqlite3
from datetime import datetime, timedelta


def init_database():
    db = sqlite3.connect("companion_data.db")
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


def save_message(db, channel, role, content, name=None):
    cursor = db.cursor()
    cursor.execute(
        "INSERT INTO messages (timestamp, channel, role, name, content) "
        "VALUES (?, ?, ?, ?, ?)",
        (datetime.now().isoformat(), channel, role, name, content)
    )
    db.commit()


def get_recent_messages(db, channel):
    cursor = db.cursor()
    cursor.execute(
        "SELECT role, name, content FROM messages "
        "WHERE channel = ? ORDER BY id",
        (channel,)
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
        "UPDATE messages SET channel = ? WHERE channel = ?",
        (new_name, old_name)
    )
    db.commit()
    return cursor.rowcount


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

    db.commit()
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
