# ============================================
# Alcove — cron.py
# Scheduled prompt runner (croniter-backed)
# Copyright (C) 2026 Robert Shea
# This software is distributed as FREEWARE. Please refer to the readme.txt file for more information.
# ============================================

import asyncio
import traceback
from datetime import datetime, timedelta, timezone

import discord.ext.tasks as tasks

import config
from .database import (
    get_db,
    list_crontab_entries,
    set_crontab_last_fired,
    increment_crontab_fail_count,
    reset_crontab_fail_count,
    delete_crontab_entry,
)
from .utils import safe_send
from . import idle


def _local_now_naive():
    # Bot convention: local time = UTC + config.TIMEZONE_OFFSET.
    # Strip tzinfo so comparisons against local-naive ISO strings (once jobs)
    # and croniter's naive fire times work without tz-mismatch errors.
    now = datetime.now(tz=timezone.utc) + timedelta(hours=config.TIMEZONE_OFFSET)
    return now.replace(tzinfo=None)


def _parse_iso_local(ts):
    # Parse a local-naive ISO datetime (e.g. "2026-07-20T14:30:00").
    # Returns None on failure.
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def _check_recurring(row, now_local):
    # Returns (should_fire: bool, prev_fire). Determines the most recent cron
    # fire time <= now_local. If it is newer than last_fired (or last_fired is
    # None), the job is due. This naturally debounces and catches up at most
    # one slot per tick, so a long offline period produces a single firing
    # rather than a flood.
    try:
        from croniter import croniter
    except ImportError as e:
        print(f"❌ [task] croniter not installed: {e}")
        return False, None

    expr = row["when"]
    base = (_parse_iso_local(row["last_fired"])
            or _parse_iso_local(row["created_at"])
            or now_local)
    try:
        cron = croniter(expr, base)
        prev_fire = cron.get_prev(datetime, start_time=now_local)
    except Exception as e:
        print(f"⚠️ [task] bad cron expression (task #{row['id']}): {expr!r} → {e}")
        return False, None

    last_fired = _parse_iso_local(row["last_fired"])
    if last_fired is not None and prev_fire <= last_fired:
        return False, prev_fire
    return True, prev_fire


def _check_once(row, now_local):
    # Returns should_fire. Fires when now_local >= stored datetime and
    # last_fired is None (so a failed prior attempt retries next tick).
    when = _parse_iso_local(row["when"])
    if when is None:
        print(f"⚠️ [task] one-time task #{row['id']} has unparseable 'when': {row['when']!r}")
        return False
    if now_local < when:
        return False
    if row["last_fired"]:
        return False
    return True


async def _post_failure_notice(row, log_label, fail_count, once):
    max_retries = getattr(config, "CRON_MAX_RETRIES", 3)
    prompt_preview = (row["prompt"][:80] + "...") if len(row["prompt"]) > 80 else row["prompt"]
    if once:
        msg = (f"⚠️ Scheduled task #{row['id']} has failed {fail_count} consecutive "
               f"time(s) and has been paused. Check the console for details.\n"
               f"Prompt: {prompt_preview}\n"
               f"Use `!enableTask {row['id']}` to retry or `!removeTask {row['id']}` to delete it.")
    else:
        msg = (f"⚠️ Scheduled task #{row['id']} has failed {fail_count} consecutive "
               f"time(s) and will skip this slot. The next scheduled run will proceed normally.\n"
               f"Prompt: {prompt_preview}")
    try:
        target = await idle._resolve_channel_by_key(row["channel"])
        if target is not None:
            await safe_send(target, msg)
        else:
            print(f"⚠️ [{log_label}] could not resolve channel '{row['channel']}' "
                  f"for failure notice")
    except Exception as e:
        print(f"⚠️ [{log_label}] failed to post failure notice: {e}")


async def _fire(db, row, companion_name, log_label, once=False):
    # Mark last_fired immediately so a slow LLM call doesn't cause the next
    # tick to re-fire the same slot. For once jobs, this also serves as the
    # "already attempted" marker (cleared on failure-within-retry-cap so we retry).
    set_crontab_last_fired(db, row["id"], _local_now_naive().isoformat())

    try:
        await idle._run_idle_prompt(
            row["channel"], row["prompt"], log_label,
            companion_name=companion_name,
        )
    except Exception as e:
        print(f"⚠️ [{log_label}] LLM run failed: {e}")
        traceback.print_exc()
        fail_count = increment_crontab_fail_count(db, row["id"])
        max_retries = getattr(config, "CRON_MAX_RETRIES", 3)
        if fail_count < max_retries:
            set_crontab_last_fired(db, row["id"], None)
            print(f"⚠️ [{log_label}] will retry next tick "
                  f"({fail_count}/{max_retries} consecutive failures)")
        else:
            reset_crontab_fail_count(db, row["id"])
            print(f"🚫 [{log_label}] retry cap exceeded ({max_retries}) — "
                  f"abandoning this slot")
            await _post_failure_notice(row, log_label, fail_count, once)
        return

    reset_crontab_fail_count(db, row["id"])

    if once:
        deleted = delete_crontab_entry(db, row["id"])
        msg = (f"✅ Task #{row['id']} executed once and automatically removed."
               if deleted
               else f"✅ Task #{row['id']} executed once (row removal failed — will retry).")
        print(msg)
        try:
            target = await idle._resolve_channel_by_key(row["channel"])
            if target is not None:
                await safe_send(target, msg)
        except Exception as e:
            print(f"⚠️ [{log_label}] failed to post removal notice: {e}")


@tasks.loop(minutes=1)
async def cron_tick_task():
    try:
        companions = idle.discover_companions()
        now_local = _local_now_naive()
        for companion_name in companions:
            db = get_db(companion_name)
            try:
                rows = list_crontab_entries(db, status="active")
            except Exception as e:
                print(f"⚠️ [task] list failed for '{companion_name}': {e}")
                continue
            for row in rows:
                et = row.get("event_type")
                try:
                    if et == "recurring":
                        should, _ = _check_recurring(row, now_local)
                        if should:
                            print(f"⏰ [task] firing recurring task #{row['id']} for "
                                  f"'{companion_name}' in '{row['channel']}' "
                                  f"(cron={row['when']!r})")
                            await _fire(db, row, companion_name,
                                        log_label=f"task#{row['id']}")
                    elif et == "once":
                        should = _check_once(row, now_local)
                        if should:
                            print(f"⏰ [task] firing one-time task #{row['id']} for "
                                  f"'{companion_name}' in '{row['channel']}' "
                                  f"(when={row['when']!r})")
                            await _fire(db, row, companion_name,
                                        log_label=f"task_once#{row['id']}", once=True)
                    else:
                        print(f"⚠️ [task] task #{row['id']} has unknown "
                              f"event_type={et!r} — skipping")
                except Exception as e:
                    print(f"⚠️ [task] task #{row['id']} failed: {e}")
                    traceback.print_exc()
    except Exception as e:
        print(f"⚠️ cron_tick_task failed: {e}")
        traceback.print_exc()


@cron_tick_task.before_loop
async def _cron_before_loop():
    # Wait until idle.init() has provided the Discord client (set in on_ready)
    # so _run_idle_prompt can resolve channels.
    while idle._client is None:
        await asyncio.sleep(1)
