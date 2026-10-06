## task (create, edit, list, or remove scheduled tasks, one-shot actions / reminders, etc.)

Schedule prompts to run automatically on a recurring schedule or once at a specific date and time. Tasks fire whether or not the user is online. The current channel is always used as the target — do NOT specify a channel.

**Syntax:**

<task action="create" event_type="recurring" when="*/5 * * * *" status="active">
The full prompt text to send to the channel goes here as the body.
</task>

<task action="create" event_type="once" when="2026-07-20T14:30:00">
A full, one-time prompt that fires at the specified date/time and then removes itself.
</task>

<task action="edit" id="7" when="0 9 * * 1">
Example of a schedule edit
</task>

<task action="edit" id="7">
Only the prompt is updated — schedule and status are unchanged.
</task>

<task action="edit" id="7" when="0 9 * * 1">
</task>

<task action="edit" id="7" status="disabled">
</task>

<task action="remove" id="7">
</task>

<task action="list">
</task>

<task action="list" status="disabled">
</task>

**Attributes (all case-insensitive):**

- `action` — required. One of: `create`, `edit`, `remove`, `list`.
- `event_type` — required for `create`. One of: `recurring`, `once`. (Ignored for `edit`/`remove` unless changing it.)
- `when` — required for `create`. Format depends on `event_type`:
  - `recurring`: a standard 5-field cron expression, space-separated: `minute hour day-of-month month day-of-week`.
    - Fields: `0-59` `0-23` `1-31` `1-12` `0-6` (0 = Sunday).
    - Supports `*` (any), `*/N` (every N), comma lists (`5,10,15`), ranges (`9-17`), and step ranges (`0-59/15`).
    - Examples:
      - `*/5 * * * *` — every 5 minutes
      - `0 9 * * 1` — every Monday at 9:00 AM
      - `0 */2 * * *` — every 2 hours on the hour
      - `30 8 1 * *` — 8:30 AM on the 1st of every month
  - `once`: a local-naive ISO-8601 datetime: `YYYY-MM-DDTHH:MM:SS` (e.g. `2026-07-20T14:30:00`). The bot's configured timezone applies; do not include a UTC offset or `Z` suffix.
- `status` — optional for `create`/`edit`. One of: `active` (default), `disabled`. A disabled task is retained but does not fire until re-enabled. For `list`, an optional filter: only tasks with that status are returned (omit to list all).
- `id` — required for `edit` and `remove`. The numeric task id (as shown by `!tasks` or `action="list"`).

**Body:**

- For `create`: the prompt text to send when the task fires. Required.
- For `edit`: the new prompt text. Optional — if the body is empty, the existing prompt is kept unchanged. Provide only the fields you want to change as attributes; omitted attributes are left as-is.
- For `remove`: ignored (leave empty).
- For `list`: ignored (leave empty).

**Notes:**

- The target channel is ALWAYS the channel where the directive is invoked. Do not include a channel field.
- `action="list"` returns the full task list (id, type, status, schedule, channel, prompt) to you as a tool response — the channel only sees a brief "Reading tasks" status. Use it to check whether a task already exists before creating a duplicate, or to look up an id before `edit`/`remove`.
- A `once` task is automatically deleted from the list after it successfully fires and posts its response. This could be useful for reminders your user might ask you to remember or set as well.
- If a `once` task's LLM call fails, it retries on the next minute tick.
- Recurring tasks that missed scheduled slots while the bot was offline fire once on the next tick (no flooding).
- DO NOT announce that you have created/edited/removed a task you actually make the call