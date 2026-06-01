"""
telegram_commands.py — Inbound Telegram bot command listener.

Runs the python-telegram-bot Application alongside the main asyncio event loop
using start()/updater.start_polling() instead of run_polling().

Command reference:
  /start_scanning       — resume all scraping jobs (persisted across restarts)
  /stop_scanning        — pause all scraping jobs (persisted)
  /status               — show job states, session ages, DB counts, current settings
  /settings             — list all scanner_config key/value pairs
  /set_interval <job> <minutes>  — change a job's run interval and reschedule
  /pause_job <job>      — pause one scraping job
  /resume_job <job>     — resume one scraping job
  /targets              — list monitored targets with pause state
  /pause <target>       — pause scraping for a specific target account
  /resume <target>      — resume scraping for a specific target account
  /scan_now <target>    — trigger immediate post scan for one target
  /digest               — send the daily digest on demand

Valid <job> names: posts, stories, highlights, mentions, followers, reposts

Security: only Telegram users whose numeric ID appears in
TELEGRAM_ALLOWED_USER_IDS (comma-separated) can issue commands.
If the env var is unset or empty, all users are allowed.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone

from loguru import logger
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from src.db import (
    get_all_config,
    get_all_targets_with_stats,
    get_config,
    get_db_stats,
    set_config,
    set_target_paused,
)

# Job id → human name mapping
_JOB_NAMES: dict[str, str] = {
    "posts": "scrape_posts",
    "stories": "scrape_stories",
    "highlights": "scrape_highlights",
    "mentions": "scrape_mentions",
    "followers": "scrape_followers",
    "reposts": "scrape_reposts",
}


# ─── Authorization ─────────────────────────────────────────────────────────────

def _load_allowed_ids() -> set[str]:
    raw = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    if not raw:
        return set()
    return {uid.strip() for uid in raw.split(",") if uid.strip()}


def _is_authorized(update: Update, allowed_ids: set[str]) -> bool:
    if not allowed_ids:
        return True
    user = update.effective_user
    return user is not None and str(user.id) in allowed_ids


async def _deny(update: Update) -> None:
    if update.callback_query:
        await update.callback_query.answer("Unauthorized.", show_alert=True)
        return
    if update.effective_message:
        await update.effective_message.reply_text("Unauthorized.")


def _d(ctx: ContextTypes.DEFAULT_TYPE):
    """Shortcut to bot_data dict."""
    return ctx.application.bot_data


def _resolve_command_target(cfg, raw_value: str, *, command_name: str) -> tuple[str, str]:
    """Resolve a Telegram target argument to one configured platform/username pair."""
    from src.main import _resolve_target_reference

    value = (raw_value or "").strip().lstrip("@")
    if not value:
        raise ValueError(f"Usage: /{command_name} <target>")
    return _resolve_target_reference(cfg, value, label=f"/{command_name} target")


async def _log_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log every inbound update so we can confirm commands are being received."""
    if update.callback_query and update.callback_query.data:
        user = update.effective_user
        logger.info(
            f"[commands] received | callback={update.callback_query.data}"
            f" | user_id={user.id if user else '?'}"
            f" | username=@{(user.username or '?') if user else '?'}"
        )
        return
    if update.message and update.message.text:
        user = update.effective_user
        cmd = update.message.text.split()[0]
        logger.info(
            f"[commands] received | command={cmd}"
            f" | user_id={user.id if user else '?'}"
            f" | username=@{(user.username or '?') if user else '?'}"
        )


# ─── /start_scanning ──────────────────────────────────────────────────────────

async def cmd_start_scanning(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, _d(context)["allowed_ids"]):
        await _deny(update); return

    conn = _d(context)["conn"]
    scheduler = _d(context).get("scheduler")

    set_config(conn, "is_scanning", "1")

    if scheduler:
        from src.main import _resume_scraping_jobs, _trigger_stories_immediately
        _resume_scraping_jobs(scheduler)
        _trigger_stories_immediately(scheduler)

    await update.message.reply_text("✅ Scanning *started*. All scraping jobs are now active.", parse_mode="Markdown")


# ─── /stop_scanning ───────────────────────────────────────────────────────────

async def cmd_stop_scanning(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, _d(context)["allowed_ids"]):
        await _deny(update); return

    conn = _d(context)["conn"]
    scheduler = _d(context).get("scheduler")

    set_config(conn, "is_scanning", "0")

    if scheduler:
        from src.main import _pause_scraping_jobs
        _pause_scraping_jobs(scheduler)

    await update.message.reply_text("⏸ Scanning *paused*. Notification delivery still runs.", parse_mode="Markdown")


# ─── /status ──────────────────────────────────────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, _d(context)["allowed_ids"]):
        await _deny(update); return

    conn = _d(context)["conn"]
    scheduler = _d(context).get("scheduler")
    cfg = _d(context)["cfg"]

    is_scanning = get_config(conn, "is_scanning", "0") == "1"
    lines = [f"*Scanner Status* — {'🟢 ACTIVE' if is_scanning else '🔴 PAUSED'}\n"]

    # Scheduler jobs
    if scheduler:
        lines.append("*Jobs:*")
        for job in scheduler.get_jobs():
            nrt = job.next_run_time
            if nrt:
                delta_s = (nrt - datetime.now(timezone.utc)).total_seconds()
                nxt = f"in {int(delta_s // 60)}m"
            else:
                nxt = "⏸ paused"
            lines.append(f"  `{job.name}` → {nxt}")

    # Session file ages
    sessions_dir = cfg.data_dir / "sessions"
    if sessions_dir.exists():
        session_files = sorted(sessions_dir.glob("*.json"))
        if session_files:
            lines.append("\n*Sessions:*")
            for sf in session_files:
                age_h = int((datetime.now().timestamp() - sf.stat().st_mtime) / 3600)
                lines.append(f"  `{sf.stem}` — {age_h}h old")

    # DB stats (non-zero tables only)
    stats = get_db_stats(conn)
    lines.append("\n*DB rows:*")
    for table, cnt in stats.items():
        if cnt > 0:
            lines.append(f"  `{table}`: {cnt}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─── /settings ────────────────────────────────────────────────────────────────

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, _d(context)["allowed_ids"]):
        await _deny(update); return

    conn = _d(context)["conn"]
    cfg_map = get_all_config(conn)

    lines = ["*Scanner Config*\n"]
    for key, val in sorted(cfg_map.items()):
        lines.append(f"  `{key}` = `{val}`")
    lines.append("\nUse /set\\_interval \\<job\\> \\<minutes\\> to change an interval\\.")
    lines.append("Use /pause\\_job or /resume\\_job \\<job\\> to toggle individual jobs\\.")

    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


# ─── /set_interval <job> <minutes> ────────────────────────────────────────────

async def cmd_set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, _d(context)["allowed_ids"]):
        await _deny(update); return

    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: /set\\_interval \\<job\\> \\<minutes\\>\n"
            f"Valid jobs: {', '.join(_JOB_NAMES.keys())}",
            parse_mode="MarkdownV2",
        )
        return

    job_key = args[0].lower()
    if job_key not in _JOB_NAMES:
        await update.message.reply_text(f"Unknown job `{job_key}`. Valid: {', '.join(_JOB_NAMES)}", parse_mode="Markdown")
        return

    try:
        minutes = int(args[1])
        if minutes < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Minutes must be a positive integer.")
        return

    conn = _d(context)["conn"]
    scheduler = _d(context).get("scheduler")
    cfg = _d(context)["cfg"]
    bot = _d(context)["bot"]
    proxy_mgr = _d(context)["proxy_mgr"]

    set_config(conn, f"{job_key}_interval_minutes", str(minutes))

    job_id = _JOB_NAMES[job_key]
    rescheduled = False
    if scheduler:
        from src.main import _reschedule_job
        rescheduled = _reschedule_job(scheduler, job_id, minutes, cfg, conn, bot, proxy_mgr)

    if rescheduled:
        await update.message.reply_text(
            f"✅ `{job_key}` interval set to *{minutes} min* and rescheduled.", parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            f"✅ Interval saved (`{job_key}` = {minutes} min). Job not running yet — will apply on next restart.", parse_mode="Markdown"
        )


# ─── /pause_job <job> ─────────────────────────────────────────────────────────

async def cmd_pause_job(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, _d(context)["allowed_ids"]):
        await _deny(update); return

    args = context.args or []
    if not args:
        await update.message.reply_text(f"Usage: /pause_job <job>\nValid: {', '.join(_JOB_NAMES)}")
        return

    job_key = args[0].lower()
    if job_key not in _JOB_NAMES:
        await update.message.reply_text(f"Unknown job `{job_key}`.", parse_mode="Markdown")
        return

    conn = _d(context)["conn"]
    scheduler = _d(context).get("scheduler")

    set_config(conn, f"{job_key}_enabled", "0")

    job_id = _JOB_NAMES[job_key]
    if scheduler:
        job = scheduler.get_job(job_id)
        if job:
            job.pause()

    await update.message.reply_text(f"⏸ Job `{job_key}` paused.", parse_mode="Markdown")


# ─── /resume_job <job> ────────────────────────────────────────────────────────

async def cmd_resume_job(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, _d(context)["allowed_ids"]):
        await _deny(update); return

    args = context.args or []
    if not args:
        await update.message.reply_text(f"Usage: /resume_job <job>\nValid: {', '.join(_JOB_NAMES)}")
        return

    job_key = args[0].lower()
    if job_key not in _JOB_NAMES:
        await update.message.reply_text(f"Unknown job `{job_key}`.", parse_mode="Markdown")
        return

    conn = _d(context)["conn"]
    scheduler = _d(context).get("scheduler")

    set_config(conn, f"{job_key}_enabled", "1")

    # Also ensure global scanning is on, otherwise the job will show as
    # resumed but won't fire
    is_scanning = get_config(conn, "is_scanning", "0") == "1"

    job_id = _JOB_NAMES[job_key]
    if scheduler and is_scanning:
        job = scheduler.get_job(job_id)
        if job:
            job.resume()

    msg = f"▶️ Job `{job_key}` enabled."
    if not is_scanning:
        msg += " Note: global scanning is paused — send /start\\_scanning first."
    await update.message.reply_text(msg, parse_mode="Markdown")


# ─── /targets ─────────────────────────────────────────────────────────────────

async def cmd_targets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, _d(context)["allowed_ids"]):
        await _deny(update); return

    conn = _d(context)["conn"]
    targets = get_all_targets_with_stats(conn)

    if not targets:
        await update.message.reply_text("No targets configured.")
        return

    # Split into chunks of 20 to avoid Telegram message limits
    chunks = [targets[i:i+20] for i in range(0, len(targets), 20)]
    for chunk in chunks:
        lines = []
        for t in chunk:
            status = "⏸" if t["is_paused"] else ("💤" if not t["is_active"] else "✅")
            last = (t["last_scraped_at"] or "never")[:16].replace("T", " ")
            lines.append(
                f"{status} `{t['platform']}/{t['username']}` posts:{t['post_count']} last:{last}"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─── /pause <username> ────────────────────────────────────────────────────────

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, _d(context)["allowed_ids"]):
        await _deny(update); return

    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /pause <target>"); return

    cfg = _d(context)["cfg"]
    conn = _d(context)["conn"]
    try:
        platform, username = _resolve_command_target(cfg, args[0], command_name="pause")
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return

    updated = set_target_paused(conn, username, is_paused=True, platform=platform)
    if updated:
        await update.message.reply_text(f"⏸ Paused `{platform}/{username}`.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"Target `{platform}/{username}` not found.", parse_mode="Markdown")


# ─── /resume <username> ───────────────────────────────────────────────────────

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, _d(context)["allowed_ids"]):
        await _deny(update); return

    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /resume <target>"); return

    cfg = _d(context)["cfg"]
    conn = _d(context)["conn"]
    try:
        platform, username = _resolve_command_target(cfg, args[0], command_name="resume")
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return

    updated = set_target_paused(conn, username, is_paused=False, platform=platform)
    if updated:
        await update.message.reply_text(f"▶️ Resumed `{platform}/{username}`.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"Target `{platform}/{username}` not found.", parse_mode="Markdown")


# ─── /scan_now <username> ─────────────────────────────────────────────────────

async def cmd_scan_now(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, _d(context)["allowed_ids"]):
        await _deny(update); return

    args = context.args or []
    if not args:
        await update.message.reply_text("Usage: /scan_now <target>"); return

    cfg = _d(context)["cfg"]
    conn = _d(context)["conn"]
    bot = _d(context)["bot"]
    proxy_mgr = _d(context)["proxy_mgr"]

    try:
        platform, username = _resolve_command_target(cfg, args[0], command_name="scan_now")
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return

    from src.main import job_scrape_posts
    target_key = {(platform, username)}
    asyncio.create_task(job_scrape_posts(cfg, conn, bot, proxy_mgr, selected_targets=target_key))
    await update.message.reply_text(f"🔍 Scan started for `{platform}/{username}`.", parse_mode="Markdown")


# ─── /digest ──────────────────────────────────────────────────────────────────

async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, _d(context)["allowed_ids"]):
        await _deny(update); return

    cfg = _d(context)["cfg"]
    conn = _d(context)["conn"]
    bot = _d(context)["bot"]

    from src.main import job_daily_digest
    asyncio.create_task(job_daily_digest(cfg, conn, bot))
    await update.message.reply_text("Sending digest...")


async def cmd_drafts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, _d(context)["allowed_ids"]):
        await _deny(update); return

    from src.digital_twin import list_pending_drafts

    drafts = list_pending_drafts(limit=12)
    if not drafts:
        await update.message.reply_text("No pending digital twin drafts.")
        return

    lines = ["Digital Twin Drafts"]
    for draft in drafts:
        lines.append(
            f"#{draft['id']} - @{draft['target_username']}"
            f" | {draft['trigger_type']}"
            f" | {draft['route']}"
            f" | {draft['review_status']}"
        )
    await update.message.reply_text("\n".join(lines))


async def cmd_draft_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, _d(context)["allowed_ids"]):
        await _deny(update); return

    args = context.args or []
    if len(args) < 3:
        await update.message.reply_text("Usage: /draft_reply <platform> <account_username> <conversation_id>")
        return

    platform = args[0].strip().lower()
    account_username = args[1].strip()
    conversation_id = args[2].strip()

    from src.digital_twin import queue_inbound_reply_draft

    draft_id = await queue_inbound_reply_draft(
        platform=platform,
        account_username=account_username,
        conversation_id=conversation_id,
        bot=_d(context)["bot"],
    )
    if draft_id is None:
        await update.message.reply_text("No draft was created. Check Gemini config, DM context, or duplicate-draft suppression.")
        return
    await update.message.reply_text(f"Queued digital twin draft #{draft_id}.")


# ─── /help ────────────────────────────────────────────────────────────────────

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update, _d(context)["allowed_ids"]):
        await _deny(update); return

    text = (
        "*Available commands:*\n\n"
        "*Global scanning:*\n"
        "  /start\\_scanning — activate all scraping jobs\n"
        "  /stop\\_scanning — pause all scraping jobs\n\n"
        "*Per-job control:*\n"
        "  /pause\\_job \\<job\\> — pause one job\n"
        "  /resume\\_job \\<job\\> — resume one job\n"
        "  /set\\_interval \\<job\\> \\<min\\> — change run interval\n\n"
        "  Valid jobs: posts, stories, highlights, mentions, followers, reposts\n\n"
        "*Per-target control:*\n"
        "  /targets — list all targets\n"
        "  /pause \\<target\\> — pause target\n"
        "  /resume \\<target\\> — resume target\n"
        "  /scan\\_now \\<target\\> — immediate scan\n\n"
        "*Info:*\n"
        "  /status — system overview\n"
        "  /settings — all config values\n"
        "  /digest — send digest now\n\n"
        "*Digital twin:*\n"
        "  /drafts — list pending AI drafts\n"
        "  /draft\\_reply \\<platform\\> \\<account\\> \\<conversation\\> — queue one AI reply draft"
    )
    await update.message.reply_text(text, parse_mode="MarkdownV2")


async def handle_digital_twin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    if not _is_authorized(update, _d(context)["allowed_ids"]):
        await _deny(update); return

    data = str(query.data or "")
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "dt":
        await query.answer()
        return

    action = parts[1].strip().lower()
    try:
        draft_id = int(parts[2])
    except ValueError:
        await query.answer("Invalid draft id.", show_alert=True)
        return

    from src.digital_twin import approve_draft, begin_edit_review, reject_draft

    actor = update.effective_user
    actor_user_id = str(actor.id) if actor else ""
    actor_username = actor.username if actor else None

    if action == "approve":
        ok = await approve_draft(
            draft_id,
            actor_user_id=actor_user_id,
            actor_username=actor_username,
            bot=_d(context)["bot"],
        )
        await query.answer("Approved and sending." if ok else "Draft could not be approved.", show_alert=not ok)
        return

    if action == "reject":
        ok = await reject_draft(
            draft_id,
            actor_user_id=actor_user_id,
            actor_username=actor_username,
            bot=_d(context)["bot"],
        )
        await query.answer("Draft rejected." if ok else "Draft could not be rejected.", show_alert=not ok)
        return

    if action == "edit":
        ok = await begin_edit_review(
            draft_id,
            actor_user_id=actor_user_id,
            actor_username=actor_username,
            bot=_d(context)["bot"],
        )
        await query.answer("Send the corrected text as your next message." if ok else "Draft could not enter edit mode.", show_alert=not ok)
        return

    await query.answer()


async def handle_digital_twin_edit_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or not update.message.text:
        return
    if update.message.text.startswith("/"):
        return
    if not _is_authorized(update, _d(context)["allowed_ids"]):
        await _deny(update); return

    from src.digital_twin import apply_edit_from_telegram_message

    actor = update.effective_user
    actor_user_id = str(actor.id) if actor else ""
    actor_username = actor.username if actor else None
    draft_id = await apply_edit_from_telegram_message(
        actor_user_id=actor_user_id,
        actor_username=actor_username,
        edited_text=update.message.text,
        chat_id=str(update.effective_chat.id) if update.effective_chat else "",
        thread_id=getattr(update.effective_message, "message_thread_id", None),
        bot=_d(context)["bot"],
    )
    if draft_id is not None:
        await update.message.reply_text(f"Updated draft #{draft_id}. Review the refreshed card before approving.")


# ─── Lifecycle ─────────────────────────────────────────────────────────────────

async def start_command_listener(
    token: str,
    cfg,
    conn,
    bot,
    proxy_mgr,
    scheduler=None,
) -> Application:
    """
    Build and start the command listener Application alongside the existing
    asyncio event loop (uses start() + updater.start_polling(), not run_polling()).
    """
    allowed_ids = _load_allowed_ids()
    if allowed_ids:
        logger.info(f"[commands] Allowed Telegram user IDs: {allowed_ids}")
    else:
        logger.warning("[commands] TELEGRAM_ALLOWED_USER_IDS not set — all users can issue commands")

    app = Application.builder().token(token).build()
    app.bot_data["cfg"] = cfg
    app.bot_data["conn"] = conn
    app.bot_data["bot"] = bot
    app.bot_data["proxy_mgr"] = proxy_mgr
    app.bot_data["scheduler"] = scheduler
    app.bot_data["allowed_ids"] = allowed_ids

    app.add_handler(CommandHandler("start_scanning", cmd_start_scanning))
    app.add_handler(CommandHandler("stop_scanning", cmd_stop_scanning))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("set_interval", cmd_set_interval))
    app.add_handler(CommandHandler("pause_job", cmd_pause_job))
    app.add_handler(CommandHandler("resume_job", cmd_resume_job))
    app.add_handler(CommandHandler("targets", cmd_targets))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("scan_now", cmd_scan_now))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("drafts", cmd_drafts))
    app.add_handler(CommandHandler("draft_reply", cmd_draft_reply))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(handle_digital_twin_callback, pattern=r"^dt:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_digital_twin_edit_message))

    # Log all inbound messages before dispatching to command handlers
    app.add_handler(TypeHandler(Update, _log_update), group=-1)

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    logger.info("[commands] Telegram command listener started")
    return app


async def stop_command_listener(app: Application) -> None:
    """Gracefully stop the command listener."""
    try:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        logger.info("[commands] Telegram command listener stopped")
    except Exception as e:
        logger.warning(f"[commands] Error stopping command listener: {e}")
