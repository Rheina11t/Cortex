"""
Family Brain – Centralised configuration.

All settings are loaded from environment variables.  A .env file is supported
via python-dotenv (loaded automatically if present in the working directory).
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Dict

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Load .env file (if present) before reading any variables
# ---------------------------------------------------------------------------
# override=True ensures .env values take precedence over system environment variables
load_dotenv(override=True)

# ---------------------------------------------------------------------------
# Logging – write to stderr so STDIO-based MCP transport is not corrupted
# ---------------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(name)-24s | %(levelname)-8s | %(message)s",
    stream=sys.stderr,
)

logger = logging.getLogger("open_brain")


# ---------------------------------------------------------------------------
# Helper: parse family members from env vars
# ---------------------------------------------------------------------------
def _parse_family_members() -> Dict[int, str]:
    """Parse FAMILY_MEMBER_N_ID / FAMILY_MEMBER_N_NAME pairs from env vars."""
    members: Dict[int, str] = {}
    for i in range(1, 20):  # support up to 20 family members
        uid = os.getenv(f"FAMILY_MEMBER_{i}_ID", "").strip()
        name = os.getenv(f"FAMILY_MEMBER_{i}_NAME", "").strip()
        if uid and name:
            try:
                members[int(uid)] = name
            except ValueError:
                logger.warning(
                    "Invalid FAMILY_MEMBER_%d_ID=%r — must be an integer.", i, uid
                )
    return members


# ---------------------------------------------------------------------------
# Validated configuration dataclass
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Settings:
    """Immutable application settings populated from environment variables."""

    # -- Multi-tenant ------------------------------------------------------
    family_id: str = field(default_factory=lambda: os.getenv("FAMILY_ID", "default"))

    # -- Supabase ----------------------------------------------------------
    supabase_url: str = field(default_factory=lambda: os.environ["SUPABASE_URL"])
    supabase_service_key: str = field(
        default_factory=lambda: os.environ["SUPABASE_SERVICE_KEY"]
    )

    # -- OpenAI (always required for embeddings) ---------------------------
    openai_api_key: str = field(
        default_factory=lambda: os.environ["OPENAI_API_KEY"]
    )
    openai_base_url: str = field(
        default_factory=lambda: os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    )
    openai_embedding_base_url: str = field(
        default_factory=lambda: os.getenv(
            "OPENAI_EMBEDDING_BASE_URL",
            "https://api.openai.com/v1",
        )
    )
    embedding_model: str = field(
        default_factory=lambda: os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
    )
    embedding_backend: str = field(
        default_factory=lambda: os.getenv("EMBEDDING_BACKEND", "openai").lower()
    )

    # -- LLM backend selection ---------------------------------------------
    # "openai"    – use OpenAI chat completions (default, e.g. gpt-4.1-mini)
    # "anthropic" – use Anthropic Messages API (e.g. claude-3-5-haiku-20241022)
    llm_backend: str = field(
        default_factory=lambda: os.getenv("LLM_BACKEND", "openai").lower()
    )

    # Model name used for metadata extraction.
    # OpenAI default:    gpt-4.1-mini
    # Anthropic default: claude-3-5-haiku-20241022
    llm_model: str = field(
        default_factory=lambda: os.getenv("LLM_MODEL", "gpt-4.1-mini")
    )

    # -- Anthropic (only required when LLM_BACKEND=anthropic) -------------
    anthropic_api_key: str = field(
        default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", "")
    )

    # -- Telegram (only required by the capture layer) ---------------------
    # Obtain from @BotFather on Telegram.
    telegram_bot_token: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", "")
    )
    # Optional comma-separated list of allowed Telegram user IDs.
    # Leave empty to allow all users (suitable for a private personal bot).
    telegram_allowed_user_ids: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")
    )

    # -- Twilio / WhatsApp (only required by the WhatsApp capture layer) ------
    # Obtain credentials from https://console.twilio.com/
    twilio_account_sid: str = field(
        default_factory=lambda: os.getenv("TWILIO_ACCOUNT_SID", "")
    )
    twilio_auth_token: str = field(
        default_factory=lambda: os.getenv("TWILIO_AUTH_TOKEN", "")
    )
    # The Twilio sandbox/production WhatsApp sender number.
    # Must include the "whatsapp:" prefix, e.g. "whatsapp:+14155238886".
    twilio_whatsapp_from: str = field(
        default_factory=lambda: os.getenv("TWILIO_WHATSAPP_FROM", "")
    )

    # -- Family Members ----------------------------------------------------
    # Parsed from FAMILY_MEMBER_N_ID / FAMILY_MEMBER_N_NAME env vars.
    # When set, only these users can interact with the bot.
    # Each memory is tagged with the family member's name.
    family_members: Dict[int, str] = field(default_factory=_parse_family_members)

    # -- Google Vision API (optional, for photo/document OCR) ---------------
    # Path to the Google Cloud service account JSON key file.
    # When empty, photo OCR is disabled and photos are stored with a
    # placeholder description.
    google_vision_key_path: str = field(
        default_factory=lambda: os.getenv("GOOGLE_VISION_KEY_PATH", "")
    )
    # Alternatively, the API key for Google Vision REST API
    google_vision_api_key: str = field(
        default_factory=lambda: os.getenv("GOOGLE_VISION_API_KEY", "")
    )

    # -- Email Capture (optional) ------------------------------------------
    family_brain_email: str = field(
        default_factory=lambda: os.getenv("FAMILY_BRAIN_EMAIL", "")
    )
    family_brain_email_password: str = field(
        default_factory=lambda: os.getenv("FAMILY_BRAIN_EMAIL_PASSWORD", "")
    )
    email_imap_host: str = field(
        default_factory=lambda: os.getenv("EMAIL_IMAP_HOST", "imap.gmail.com")
    )
    email_imap_port: int = field(
        default_factory=lambda: int(os.getenv("EMAIL_IMAP_PORT", "993"))
    )
    email_poll_interval: int = field(
        default_factory=lambda: int(os.getenv("EMAIL_POLL_INTERVAL_SECONDS", "300"))
    )
    email_allowed_senders: str = field(
        default_factory=lambda: os.getenv("EMAIL_ALLOWED_SENDERS", "")
    )

    # -- MCP Server --------------------------------------------------------
    mcp_transport: str = field(
        default_factory=lambda: os.getenv("MCP_TRANSPORT", "stdio")
    )
    mcp_host: str = field(
        default_factory=lambda: os.getenv("MCP_HOST", "127.0.0.1")
    )
    mcp_port: int = field(
        default_factory=lambda: int(os.getenv("MCP_PORT", "8000"))
    )
    mcp_auth_token: str = field(
        default_factory=lambda: os.getenv("MCP_AUTH_TOKEN", "")
    )

    # -- OAuth 2.0 (for ChatGPT and other OAuth-capable MCP clients) ------
    oauth_user_password: str = field(
        default_factory=lambda: os.getenv("OAUTH_USER_PASSWORD", "")
    )
    oauth_server_url: str = field(
        default_factory=lambda: os.getenv("OAUTH_SERVER_URL", "")
    )
    oauth_token_ttl: int = field(
        default_factory=lambda: int(os.getenv("OAUTH_TOKEN_TTL", "86400"))
    )

    # -- Mailgun Inbound Email (optional) ---------------------------------
    # Required to receive inbound emails forwarded to {family_id}@familybrain.co.
    # Obtain from: https://app.mailgun.com/app/account/security/api_keys
    mailgun_api_key: str = field(
        default_factory=lambda: os.getenv("MAILGUN_API_KEY", "")
    )
    # The signing key used to verify Mailgun webhook payloads.
    # Found in: Mailgun Dashboard -> Webhooks -> HTTP webhook signing key
    mailgun_webhook_signing_key: str = field(
        default_factory=lambda: os.getenv("MAILGUN_WEBHOOK_SIGNING_KEY", "")
    )
    # The Mailgun domain configured for inbound routing (e.g. familybrain.co).
    mailgun_domain: str = field(
        default_factory=lambda: os.getenv("MAILGUN_DOMAIN", "familybrain.co")
    )

    # -- Daily Digest (optional) -------------------------------------------
    # Telegram user IDs to receive the daily digest (comma-separated).
    # Defaults to all family member IDs if not set.
    digest_recipient_ids: str = field(
        default_factory=lambda: os.getenv("DIGEST_RECIPIENT_IDS", "")
    )

    # -----------------------------------------------------------------------
    # Validation methods
    # -----------------------------------------------------------------------
    def validate_telegram(self) -> None:
        """Raise if the Telegram bot token is missing."""
        if not self.telegram_bot_token:
            raise EnvironmentError(
                "Missing required environment variable: TELEGRAM_BOT_TOKEN\n"
                "Create a bot via @BotFather on Telegram and set the token in your .env file."
            )

    def validate_twilio(self) -> None:
        """Raise if required Twilio credentials are missing."""
        missing = []
        if not self.twilio_account_sid:
            missing.append("TWILIO_ACCOUNT_SID")
        if not self.twilio_auth_token:
            missing.append("TWILIO_AUTH_TOKEN")
        if not self.twilio_whatsapp_from:
            missing.append("TWILIO_WHATSAPP_FROM")
        if missing:
            raise EnvironmentError(
                f"Missing required Twilio environment variable(s): {', '.join(missing)}\n"
                "Obtain these from https://console.twilio.com/ and set them in your .env file."
            )

    def validate_llm_backend(self) -> None:
        """Raise if LLM_BACKEND is invalid or required keys are missing."""
        valid = {"openai", "anthropic"}
        if self.llm_backend not in valid:
            raise ValueError(
                f"Invalid LLM_BACKEND={self.llm_backend!r}. "
                f"Must be one of: {', '.join(sorted(valid))}"
            )
        if self.llm_backend == "anthropic" and not self.anthropic_api_key:
            raise EnvironmentError(
                "LLM_BACKEND=anthropic requires ANTHROPIC_API_KEY to be set."
            )

    def validate_mcp_transport(self) -> None:
        """Raise if the transport value is not recognised."""
        valid = {"stdio", "http", "sse"}
        if self.mcp_transport not in valid:
            raise ValueError(
                f"Invalid MCP_TRANSPORT={self.mcp_transport!r}. "
                f"Must be one of: {', '.join(sorted(valid))}"
            )

    def validate_oauth(self) -> None:
        """Warn if OAuth is partially configured."""
        if self.oauth_user_password and not self.oauth_server_url:
            logger.warning(
                "OAUTH_USER_PASSWORD is set but OAUTH_SERVER_URL is not. "
                "The server will derive the public URL from request headers, "
                "which may not work behind all reverse proxies."
            )

    def validate_email(self) -> None:
        """Raise if email capture is partially configured."""
        if self.family_brain_email and not self.family_brain_email_password:
            raise EnvironmentError(
                "FAMILY_BRAIN_EMAIL is set but FAMILY_BRAIN_EMAIL_PASSWORD is missing."
            )

    def has_google_vision(self) -> bool:
        """Return True if Google Vision is configured."""
        return bool(self.google_vision_key_path or self.google_vision_api_key)

    def has_email_capture(self) -> bool:
        """Return True if email capture is configured."""
        return bool(self.family_brain_email and self.family_brain_email_password)

    def has_mailgun_inbound(self) -> bool:
        """Return True if Mailgun inbound email is configured."""
        return bool(self.mailgun_webhook_signing_key and self.mailgun_domain)

    def get_digest_recipients(self) -> list[int]:
        """Return the list of Telegram user IDs for the daily digest."""
        if self.digest_recipient_ids:
            return [
                int(uid.strip())
                for uid in self.digest_recipient_ids.split(",")
                if uid.strip()
            ]
        # Default to all family members
        return list(self.family_members.keys())


def get_settings() -> Settings:
    """Return a validated Settings instance (raises on missing required vars)."""
    try:
        return Settings()
    except KeyError as exc:
        logger.critical("Missing required environment variable: %s", exc)
        raise SystemExit(1) from exc
