#!/usr/bin/env python3
"""
Open Brain – Panning for Gold.

A weekly insight engine that looks across all memories (or a configurable
window), finds non-obvious connections, recurring themes, and hidden patterns,
and sends them to Telegram every Sunday morning at 09:00.

Unlike the Daily Digest (which summarises recent events), Panning for Gold
deliberately looks for things you might have missed — cross-day connections,
ideas that appeared multiple times in different contexts, and emerging themes.

Usage:
    # Run once immediately (useful for testing):
    python -m src.panning_for_gold --now

    # Start the scheduler (runs every Sunday at 09:00):
    python -m src.panning_for_gold

Required environment variables (see .env.example):
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    SUPABASE_URL, SUPABASE_SERVICE_KEY,
    OPENAI_API_KEY
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from openai import OpenAI
from telegram import Bot

from .config import get_settings, logger as root_logger
from . import brain

logger = logging.getLogger("open_brain.gold")

# ---------------------------------------------------------------------------
# Insight generation
# ---------------------------------------------------------------------------

_GOLD_SYSTEM_PROMPT = """\
You are a personal insight generator for a professional called Dan.
You will receive a list of his memories (notes, ideas, meeting summaries) spanning several weeks.

Your job is to find the GOLD — the non-obvious insights that Dan probably hasn't noticed himself.

Produce exactly 4 insights in this format:

INSIGHT 1: [one-line title]
[2-3 sentences explaining the specific connection or pattern you found, referencing actual content from the memories]

INSIGHT 2: [one-line title]
[2-3 sentences...]

INSIGHT 3: [one-line title]
[2-3 sentences...]

INSIGHT 4: [one-line title]
[2-3 sentences...]

Rules:
- Be SPECIFIC. Reference actual topics, people, or dates from the memories.
- Do NOT produce generic insights like "You think about AI a lot."
- Look for: ideas that appeared in different contexts, contradictions, emerging patterns, things mentioned multiple times weeks apart, connections between seemingly unrelated topics.
- Use plain text only — no markdown, no asterisks.
- Keep each insight under 60 words.
"""


def _format_memories_for_gold(memories: list[dict]) -> str:
    """Format memories for the Panning for Gold prompt — more compact than digest."""
    parts = []
    for m in memories:
        meta = m.get("metadata") or {}
        date = m.get("created_at", "")[:10]
        category = meta.get("category", "?")
        tags = ", ".join(meta.get("tags", []))
        thought_type = meta.get("thought_type", "")
        content = m.get("content", "")[:200]

        line = f"[{date}] [{category}]"
        if thought_type:
            line += f" [{thought_type}]"
        if tags:
            line += f" | Tags: {tags}"
        line += f"\n{content}"
        parts.append(line)
    return "\n\n".join(parts)


def generate_gold_text(memories: list[dict], settings) -> str:
    """Call the LLM to surface insights from a list of memories."""
    if len(memories) < 5:
        return "Not enough memories yet to find patterns. Keep capturing thoughts and check back next week!"

    memory_text = _format_memories_for_gold(memories)
    client = OpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
    )
    response = client.chat.completions.create(
        model=settings.llm_model,
        temperature=0.6,
        messages=[
            {"role": "system", "content": _GOLD_SYSTEM_PROMPT},
            {"role": "user", "content": f"Find the gold in these {len(memories)} memories:\n\n{memory_text}"},
        ],
        max_tokens=600,
    )
    return response.choices[0].message.content or "Could not generate insights."


# ---------------------------------------------------------------------------
# Telegram delivery
# ---------------------------------------------------------------------------

async def send_gold(settings) -> None:
    """Fetch memories, generate insights, and send to Telegram."""
    chat_id_str = os.getenv("TELEGRAM_CHAT_ID", "")
    if not chat_id_str:
        logger.error("TELEGRAM_CHAT_ID not set — cannot send Panning for Gold.")
        return

    chat_id = int(chat_id_str)
    bot = Bot(token=settings.telegram_bot_token)

    # Use last 30 days for a rich cross-temporal analysis
    memories = brain.list_memories_since(hours=720)  # 30 days
    if len(memories) < 5:
        memories = brain.list_recent_memories(limit=50)  # fallback: all time

    now_str = datetime.now(timezone.utc).strftime("%A, %d %b %Y")
    header = f"⛏ Panning for Gold — {now_str}\n({len(memories)} memories analysed)\n\n"

    try:
        gold_body = generate_gold_text(memories, settings)
        message = header + gold_body
    except Exception as exc:
        logger.error("Gold generation failed: %s", exc)
        message = header + f"Could not generate insights: {exc}"

    await bot.send_message(chat_id=chat_id, text=message)
    logger.info("Panning for Gold sent to chat_id=%s", chat_id)


# ---------------------------------------------------------------------------
# Scheduler entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Open Brain – Panning for Gold")
    parser.add_argument(
        "--now",
        action="store_true",
        help="Send insights immediately and exit (for testing).",
    )
    parser.add_argument(
        "--hour",
        type=int,
        default=9,
        help="Hour of day to send insights (24h format, default: 9).",
    )
    parser.add_argument(
        "--minute",
        type=int,
        default=0,
        help="Minute of hour to send insights (default: 0).",
    )
    args = parser.parse_args()

    settings = get_settings()
    settings.validate_telegram()
    brain.init(settings)

    if args.now:
        logger.info("Sending Panning for Gold immediately (--now flag)…")
        asyncio.run(send_gold(settings))
        return

    # Schedule weekly on Sunday
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        lambda: asyncio.run(send_gold(settings)),
        trigger=CronTrigger(day_of_week="sun", hour=args.hour, minute=args.minute),
        id="panning_for_gold",
        name="Open Brain Panning for Gold",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(
        "Panning for Gold scheduler started — will send every Sunday at %02d:%02d.",
        args.hour, args.minute,
    )

    try:
        import time
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Gold scheduler stopped.")
        scheduler.shutdown()


if __name__ == "__main__":
    main()
