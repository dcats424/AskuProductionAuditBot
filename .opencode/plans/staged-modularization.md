# Staged Modularization of bot.py (7,092 lines → package)

## Goal
Split the monolith into a Python package with clean module boundaries.
**Same framework** (python-telegram-bot 20.6), **zero behavior change**,
**staged** so the bot stays runnable and testable after every stage.

## Golden rules (every stage)
1. Move code **verbatim** — no logic edits while moving (only import fixes).
2. `python3 -m py_compile` + boot smoke test after each stage.
3. User restarts the bot after each stage to verify live behavior.
4. `git commit` per stage (bot.log / __pycache__ excluded).

## Dependency direction (prevents circular imports)
config → db → state → messaging → scheduler → reminders → handlers/ai/kpis
→ main. Nothing imports `main`; only `main` imports handlers.

---

## Stage 0 — Baseline + dead-code cleanup (small, safe)
- Delete **duplicate `parse_vos`** (bot.py:736) — the live copy (bot.py:1276)
  is used by all 7 call sites (2208, 3045, 3263, 3522, 4582, 5039, 6836).
- Delete **dead `parse_downtime`** (bot.py:1204) — zero callers; everything
  uses `parse_downtime_categorized` (2879).
- Remove unused **APScheduler** from requirements.txt (never imported; the bot
  uses telegram's JobQueue).
- Remove unused **reportlab** from requirements.txt (never imported — all
  weekly/monthly reports are generated as Telegram text messages via
  `split_and_send_long_message` (bot.py:414), NOT PDFs).
- Delete `ai_test.py` scratch file (confirm with user).
- Baseline verify + commit.

## Stage 1 — Foundation: `config.py`, `db.py`, `state.py`
- **config.py**: Ethiopian time helpers (`now_ethiopia`, `to_ethiopian_clock`,
  `ethiopian_clock_time_to_pc_time`, `get_shift_for_time`,
  `get_current_hour_number`, `format_date_time_12h`, `format_hour_range_12h`),
  all constants (LINE_STATE_*, window minutes, REMINDER_*, BOT_STATUS_*,
  BOT_STATUS_AUTODELETE_SECONDS, BOT_TOKEN, TZ_ETHIOPIA), line config
  (`configured_lines`, `chat_id_for_line`, `line_key_for_chat`,
  `db_name_for_line`, `default_chat_id`, `is_allowed_chat`).
- **db.py**: `db_config_for_chat`, `get_db_connection`, `get_clean_db_connection`,
  `bot_state_get/set`, `save_to_database`, `save_hourly_to_database`, queries.
- **state.py**: `LineRuntime`, `line_runtime`, `line_runtime_for_line`,
  `load_bot_state_from_db`.
- bot.py keeps everything else; imports from the three.
- Verify + commit.

## Stage 2 — `messaging.py` + `scheduler.py`
- **messaging.py**: `send_or_queue_reminder`, `flush_pending_reminders`,
  `split_and_send_long_message`, `_record_reminder_message`,
  `delete_reminder_frame`, `_delete_bot_status_msg`,
  `_delete_bot_status_after_delay`, `_schedule_bot_status_autodelete`,
  `_try_delete_message`.
- **scheduler.py**: `setup_shift_schedules`, `is_in_hourly_plan_window`,
  `is_in_hourly_summary_window`, `is_in_shift_summary_window`,
  `get_shift_reminders`, reminder-minute constants.
- Verify + commit.

## Stage 3 — `reminders.py`
- `remind_daily_production_plan`, `remind_shift_plan`, `remind_shift_report`,
  `remind_hourly_plan`, `remind_hourly_summary`,
  `send_daily_plan_if_needed`, `send_shift_plan_if_needed`,
  `send_current_hour_plan`, `send_current_hour_summary`,
  `catch_up_missed_reminders`, `recover_missed_reminders_on_reconnect`,
  `handle_partial_hours_on_line_resume`, `connection_watchdog`.
- Verify + commit.

## Stage 4 — Handlers, AI, parsing, KPIs, reports, `main.py`
- **handlers.py**: `line_off/on`, `sanitation_start/end`, hourly data input
  flow, `hourly_summary_ai_cmd`, `shift_summary_hourly_1/2_cmd`,
  `all_shift_summary_from_hourly_cmd`, `weekly_report_cmd`,
  `monthly_report_cmd`, `bot_status_cmd`.
- **ai.py**: all `ai_generate_*`, `_post_validation_recap`, validation helpers.
- **parsing.py**: `parse_report`, `parse_downtime_categorized`,
  `parse_rejects`, `parse_operator_notes`, `detect_repeated_faults`,
  `parse_vos` (live copy).
- **kpis.py**: `compute_kpis`, `compute_risk_assessment`,
  `get_pcs_per_pack`, `aggregate_period_from_db`,
  `calculate_expected_production`, `get_shift_duration_minutes`,
  `get_default_production_hours`.
- **reports.py**: weekly/monthly/shift report **TEXT builders** posted as
  Telegram messages (`generate_shift_summary_from_hourly`,
  `generate_multi_shift_summary_and_post`, `build_downtime_analysis_block`,
  `format_downtime_category_block`, `format_vos_duration`,
  `flatten_categorized_downtime`). **No PDFs — reports are sent in the group.**
- **main.py**: app build (ApplicationBuilder), error handler, ALL
  CommandHandler registrations, `post_init`, entrypoint.
- Delete bot.py. Full verify + live run + commit.

## Out of scope
- No behavior changes, no new features, no framework switch, no DB changes.
- The 2-min autodelete, :05 H1 plan, and gate removals land as-is in the
  refactored modules (they are already committed behavior).

## Risks & mitigations
- **Circular imports** → strict dependency direction above.
- **Regression** → verbatim moves + diff review per stage + live restart.
- **Live bot downtime** → only during each restart; catch-up reminders may
  fire on restart (existing, expected behavior).
- **bot_state/DB keys** → untouched; no schema changes.
