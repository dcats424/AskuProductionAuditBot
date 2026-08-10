import asyncio
import logging

from telegram.ext import ContextTypes

from config import (
    BOT_STATUS_AUTODELETE_SECONDS,
    BOT_STATUS_MSG_KEY,
    HOURLY_PROMPT_MSG_KEY,
    HOURLY_SUMMARY_MSG_KEY,
    HOURLY_TWOSTEP_CMD_MSG_KEY,
    HOURLY_VALIDATION_RECAP_MSG_KEY,
)
from db import bot_state_get, bot_state_set

logger = logging.getLogger(__name__)


async def split_and_send_long_message(bot, chat_id: int, text: str, parse_mode: str | None = None) -> None:
    """
    Send a message, splitting into chunks of 4096 chars if needed.
    Splits at natural boundaries (section dividers) when possible.
    """
    MAX_LENGTH = 4096

    if len(text) <= MAX_LENGTH:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
        return

    # Split at section dividers
    DIVIDER = "────────────────────────────"
    sections = text.split(DIVIDER)

    chunks = []
    current_chunk = ""

    for section in sections:
        section = section.strip()
        if not section:
            continue

        # Build test chunk
        if current_chunk:
            test_chunk = current_chunk + "\n\n" + DIVIDER + "\n\n" + section
        else:
            test_chunk = section

        if len(test_chunk) > MAX_LENGTH and current_chunk:
            chunks.append(current_chunk)
            current_chunk = section
        else:
            current_chunk = test_chunk

    if current_chunk:
        chunks.append(current_chunk)

    # Hard split any oversized chunks
    final_chunks = []
    for chunk in chunks:
        if len(chunk) <= MAX_LENGTH:
            final_chunks.append(chunk)
        else:
            for i in range(0, len(chunk), MAX_LENGTH):
                final_chunks.append(chunk[i:i+MAX_LENGTH])

    # Send all chunks
    for i, chunk in enumerate(final_chunks):
        await bot.send_message(chat_id=chat_id, text=chunk, parse_mode=parse_mode)
        if i < len(final_chunks) - 1:
            await asyncio.sleep(0.5)



# ── Chat message auto-cleanup helpers ────────────────────────────────────────
PENDING_FAILED_MSGS_KEY = "pending_failed_msgs"
MAX_PENDING_FAILED_MSGS = 10


async def _try_delete_message(bot, chat_id: int | None, message_id) -> None:
    """Best-effort message deletion. Never crashes — failures are logged only."""
    if not chat_id or not isinstance(message_id, int):
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logger.warning(f"Could not delete message id={message_id} in chat {chat_id}: {e}")


async def _delete_or_queue(bot, chat_id: int | None, message_id) -> None:
    """Delete a message; if deletion fails (e.g. bad internet), queue it so the
    next successful command run retries."""
    if not chat_id or not isinstance(message_id, int):
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logger.warning(f"Could not delete message id={message_id} (queued for retry): {e}")
        _queue_failed_messages(chat_id, message_id)


def _queue_failed_messages(chat_id: int | None = None, *msg_ids) -> None:
    """Record failed-attempt message ids; they are deleted at the next success."""
    if chat_id is None:
        return
    seen = set()
    current = bot_state_get(PENDING_FAILED_MSGS_KEY, chat_id) or ""
    for chunk in current.split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            seen.add(int(chunk))
    for mid in msg_ids:
        if isinstance(mid, int):
            seen.add(mid)
    limited = list(seen)[:MAX_PENDING_FAILED_MSGS]
    bot_state_set(PENDING_FAILED_MSGS_KEY, ",".join(str(i) for i in limited), chat_id)


async def _purge_failed_messages(bot, chat_id: int | None = None) -> None:
    """Delete all recorded failed-attempt messages; failures are re-queued for
    the next retry so no escaped command is ever left behind."""
    if chat_id is None:
        return
    current = bot_state_get(PENDING_FAILED_MSGS_KEY, chat_id) or ""
    if not current:
        return
    retry = []
    for chunk in current.split(","):
        chunk = chunk.strip()
        if not chunk.isdigit():
            continue
        try:
            await bot.delete_message(chat_id=chat_id, message_id=int(chunk))
        except Exception as e:
            logger.warning(f"Could not purge message id={chunk} (kept for retry): {e}")
            retry.append(int(chunk))
    if retry:
        bot_state_set(
            PENDING_FAILED_MSGS_KEY,
            ",".join(str(i) for i in retry[:MAX_PENDING_FAILED_MSGS]),
            chat_id,
        )
    else:
        bot_state_set(PENDING_FAILED_MSGS_KEY, "", chat_id)


async def _cleanup_command_after_success(
    bot, chat_id: int | None, command_msg_id: int | None, *extra_msg_ids
) -> None:
    """After a successful report: purge failed attempts, then delete the command
    message and any progress/confirmation messages. Failures are queued for retry."""
    await _purge_failed_messages(bot, chat_id)
    await _delete_or_queue(bot, chat_id, command_msg_id)
    for msg_id in extra_msg_ids:
        await _delete_or_queue(bot, chat_id, msg_id)


def _store_hourly_two_step_ids(
    chat_id: int | None, command_msg_id: int | None, prompt_msg_id: int | None
) -> None:
    """Remember the two-step hourly prompt + command message ids for cleanup."""
    if chat_id is None:
        return
    bot_state_set(HOURLY_TWOSTEP_CMD_MSG_KEY, str(command_msg_id or ""), chat_id)
    bot_state_set(HOURLY_PROMPT_MSG_KEY, str(prompt_msg_id or ""), chat_id)


async def _cleanup_hourly_two_step(bot, chat_id: int | None = None) -> None:
    """Delete the stored two-step /hourly_summary_ai command + prompt messages."""
    if chat_id is None:
        return
    for key in (HOURLY_TWOSTEP_CMD_MSG_KEY, HOURLY_PROMPT_MSG_KEY):
        raw = bot_state_get(key, chat_id) or ""
        if raw.isdigit():
            await _delete_or_queue(bot, chat_id, int(raw))
        bot_state_set(key, "", chat_id)


async def post_hourly_summary(bot, chat_id: int | None, text: str) -> None:
    """Post an hourly summary; deletes the previous one so only the latest stays."""
    if chat_id is None:
        return
    sent = await bot.send_message(chat_id=chat_id, text=text)
    previous = bot_state_get(HOURLY_SUMMARY_MSG_KEY, chat_id) or ""
    if previous.isdigit():
        await _try_delete_message(bot, chat_id, int(previous))
    bot_state_set(HOURLY_SUMMARY_MSG_KEY, str(sent.message_id), chat_id)


async def post_validation_recap(
    bot, chat_id: int | None, text: str, parse_mode: str | None = None
) -> None:
    """Post a validation recap; deletes the previous one so only the latest stays."""
    if chat_id is None:
        return
    sent = await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
    previous = bot_state_get(HOURLY_VALIDATION_RECAP_MSG_KEY, chat_id) or ""
    if previous.isdigit():
        await _try_delete_message(bot, chat_id, int(previous))
    bot_state_set(HOURLY_VALIDATION_RECAP_MSG_KEY, str(sent.message_id), chat_id)


async def _delete_bot_status_msg(bot, chat_id: int | None = None) -> None:
    """Delete the previously posted /status message (if any) and clear the key."""
    if chat_id is None:
        return
    raw = bot_state_get(BOT_STATUS_MSG_KEY, chat_id) or ""
    if raw.isdigit():
        await _try_delete_message(bot, chat_id, int(raw))
    bot_state_set(BOT_STATUS_MSG_KEY, "", chat_id)


async def _delete_bot_status_after_delay(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete the /bot_status message (and its command message) 2 minutes after posting."""
    data = context.job.data or {}
    chat_id = data.get("chat_id")
    if not chat_id:
        return
    message_id = data.get("message_id")
    if message_id:
        await _try_delete_message(context.bot, chat_id, message_id)
        if bot_state_get(BOT_STATUS_MSG_KEY, chat_id) == str(message_id):
            bot_state_set(BOT_STATUS_MSG_KEY, "", chat_id)
    command_message_id = data.get("command_message_id")
    if command_message_id:
        await _try_delete_message(context.bot, chat_id, command_message_id)


def _schedule_bot_status_autodelete(
    job_queue,
    message_id: int,
    chat_id: int,
    command_message_id: int | None = None,
    delay_seconds: int = BOT_STATUS_AUTODELETE_SECONDS,
) -> None:
    """Schedule deletion of a status message (and its /bot_status command message) after delay_seconds."""
    if job_queue is None or not chat_id or not message_id:
        return
    job_queue.run_once(
        _delete_bot_status_after_delay,
        delay_seconds,
        data={
            "chat_id": chat_id,
            "message_id": message_id,
            "command_message_id": command_message_id,
        },
        name="bot_status_autodelete",
    )


