#!/usr/bin/env python3
"""
Open Brain – Slack Capture Layer.

A Slack Bolt application (Socket Mode) that listens for messages in
designated channels, processes them through an LLM for metadata extraction,
generates vector embeddings, stores everything in Supabase, and posts a
confirmation back to Slack.

Usage:
    python -m src.slack_capture          # from the project root
    python src/slack_capture.py          # direct invocation

Required environment variables (see .env.example):
    SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET, SLACK_APP_TOKEN,
    SUPABASE_URL, SUPABASE_SERVICE_KEY, OPENAI_API_KEY
"""

from __future__ import annotations

import logging
import sys
import traceback

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from .config import get_settings, logger as root_logger
from . import brain

logger = logging.getLogger("open_brain.slack")

# ---------------------------------------------------------------------------
# Initialise settings and core brain module
# ---------------------------------------------------------------------------
settings = get_settings()
settings.validate_slack()
brain.init(settings)

# ---------------------------------------------------------------------------
# Create the Slack Bolt application
# ---------------------------------------------------------------------------
app = App(
    token=settings.slack_bot_token,
    signing_secret=settings.slack_signing_secret,
)


# ---------------------------------------------------------------------------
# Event handler: capture messages
# ---------------------------------------------------------------------------
@app.event("message")
def handle_message(event: dict, say, client) -> None:
    """
    Process every new message in channels the bot is invited to.

    Workflow:
      1. Ignore bot messages and message edits/deletes.
      2. Send the raw text to the LLM for cleaning + metadata extraction.
      3. Generate a vector embedding of the cleaned text.
      4. Store the memory in Supabase.
      5. React with a ✅ emoji and post a threaded confirmation.
    """
    # -- Guard clauses ----------------------------------------------------
    subtype = event.get("subtype")
    if subtype in ("bot_message", "message_changed", "message_deleted", "channel_join"):
        return

    raw_text: str = event.get("text", "").strip()
    if not raw_text:
        return

    channel: str = event.get("channel", "")
    ts: str = event.get("ts", "")
    user: str = event.get("user", "unknown")

    logger.info("Received message from user=%s in channel=%s", user, channel)

    try:
        # -- Step 1: Extract metadata via LLM ----------------------------
        extracted = brain.extract_metadata(raw_text)
        cleaned_content: str = extracted.pop("cleaned_content", raw_text)

        # Enrich metadata with Slack context
        extracted["slack_user"] = user
        extracted["slack_channel"] = channel
        extracted["slack_ts"] = ts

        # -- Step 2: Generate embedding -----------------------------------
        embedding = brain.generate_embedding(cleaned_content)

        # -- Step 3: Store in Supabase ------------------------------------
        record = brain.store_memory(
            content=cleaned_content,
            embedding=embedding,
            metadata=extracted,
        )

        memory_id = record.get("id", "n/a")
        tags = extracted.get("tags", [])
        category = extracted.get("category", "other")

        # -- Step 4: Confirm back to Slack --------------------------------
        # Add a checkmark reaction
        try:
            client.reactions_add(channel=channel, name="brain", timestamp=ts)
        except Exception:
            # Fallback if :brain: emoji is not available
            try:
                client.reactions_add(channel=channel, name="white_check_mark", timestamp=ts)
            except Exception:
                pass  # non-critical

        # Post a threaded confirmation
        confirmation = (
            f"🧠 *Memory captured!*\n"
            f"• *Category:* {category}\n"
            f"• *Tags:* {', '.join(tags) if tags else 'none'}\n"
            f"• *ID:* `{memory_id}`"
        )
        say(text=confirmation, thread_ts=ts)

        logger.info("Memory captured successfully (id=%s)", memory_id)

    except Exception as exc:
        logger.error("Failed to capture memory: %s\n%s", exc, traceback.format_exc())
        say(
            text=f"⚠️ Failed to capture memory: {exc}",
            thread_ts=ts,
        )


# ---------------------------------------------------------------------------
# Health-check: respond to app_mention
# ---------------------------------------------------------------------------
@app.event("app_mention")
def handle_mention(event: dict, say) -> None:
    """Respond when the bot is @-mentioned (useful for health checks)."""
    say(
        text="🧠 Open Brain is online and listening! Send a message in this channel to capture a memory.",
        thread_ts=event.get("ts"),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    """Start the Slack Bolt app in Socket Mode."""
    logger.info("Starting Open Brain Slack capture layer (Socket Mode)…")
    handler = SocketModeHandler(app, settings.slack_app_token)
    handler.start()


if __name__ == "__main__":
    main()
