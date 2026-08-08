# Fix: Pops must only be gated by line state (OFF/SANITATION) and AI audit

## Desired rule (user-confirmed)
Pop reminders are **not associated with production data**.
- Line RUNNING → every pop fires at its scheduled minute.
- Line OFF / SANITATION → suppressed by line state.
- AI audit block → queued, delivered after the audit (flush confirmed at
  bot.py:4853/6767) — "the suppressed ones will then come".

## Current violations (rogue production gates — must be removed)
1. `remind_hourly_summary` (bot.py:5242-5248): `:55`/`:50` pop skipped +
   **permanently marked done** when `_hour_had_production_or_partial()` is
   False. (Deadlock: the pop is the prompt to submit data, but requires data
   first.)
2. `remind_shift_report` (bot.py:5132-5135): `18:55`/`06:55` handoff pop
   skipped + marked done when `_shift_had_any_production()` is False.
3. Recovery `recover_missed_reminders_on_reconnect` (bot.py:1957, 2008):
   hourly summary + shift report gated on `shift_has_production`.
4. Startup catch-up `catch_up_missed_reminders` (bot.py:5964, 6011): same.

## Changes (bot.py)

### 1. `remind_hourly_summary` — delete the gate block (5242-5248)
Always pop at `:55` (`:50` hour 12). Window, dedupe keys, line-state
suppression via `send_or_queue_reminder` all stay.

### 2. `remind_shift_report` — delete the gate block (5132-5135)
Always pop at 18:55 / 06:55.

### 3. Recovery — hourly summary (1956-2002): send unconditionally
Remove the `if shift_has_production:` / `else: skipped` wrapper, de-indent the
send block. CASE 1 early-return (bot.py:1822) stays (line-state based).

### 4. Recovery — shift summary (2007-2038): send unconditionally
Remove wrapper + delete the `else` branch that pre-writes
`shift_report_fired_...` (which would block the scheduler's retry).

### 5. Startup catch-up — hourly summary (5963-6007): send unconditionally
Same as #3.

### 6. Startup catch-up — shift summary (6010-6040): send unconditionally
Same as #4.

### 7. CASE 1 DB fallback in `send_or_queue_reminder` (bot.py:1539)
`rt.shift_had_production.get(shift_now, False)` is in-memory only; after a
restart with the line OFF it is always False → over-suppresses everything
even when production exists in DB. Add the DB fallback (same pattern as
recovery at bot.py:1810):
```python
if not rt.shift_had_production.get(shift_now, False) and not _shift_had_any_production(shift_now, now.date().isoformat(), chat_id):
```
Suppresses only when the DB also shows zero production. Consistent with the
user's rule (suppression = line state only).

## Kept intact
- Strict windows: plans `:05`(H1)/`:02`(H2-12) to `:30`, summaries `:55-:59`
  (`:50-:59` H12), shift report `:55-:59` — no late sends.
- Dedupe keys (once per hour/day).
- **Reminder message deletion after the hour ends** (user-confirmed
  requirement):
  - When hour N+1's plan pops, `remind_hourly_plan` calls
    `delete_reminder_frame` (bot.py:5213-5215) which deletes hour N's plan AND
    summary messages (tracked via `_record_reminder_message` bot.py:1641,
    deleted at bot.py:1660-1680). Recovery does the same (bot.py:1937-1938).
  - `flush_pending_reminders` drops expired frames instead of sending them
    (bot.py:1687).
  - **None of the 7 edits touch these paths** — with the gates removed the
    summary now always sends, so it is always tracked and always cleaned up
    when the next hour starts.
- Line-state rules in `send_or_queue_reminder`: CASE 1 (OFF/sanitation entire
  shift, zero production → suppress), CASE 2 (OFF mid-shift → plans
  suppressed, one hourly reminder allowed, shift report never suppressed).
- AI audit: queue + flush on audit end / auto-release timeout.
- `_hour_had_production_or_partial` / `_shift_had_any_production` remain
  (still used by CASE 1 logic).

## Verification
1. `python3 -m py_compile bot.py`
2. Harness import test: confirm the gate call sites are gone from the 4 pop
   paths; confirm windows/job times unchanged (07:05/19:05 H1 plans, `:02`
   others, summaries `:55`/`:50`, reports `:55`).
3. User restarts bot; with line RUNNING every pop must arrive at its minute
   (terminal logs `Hourly summary fired: Shift X Hour Y` even with no data).
