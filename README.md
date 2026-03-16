# Open Brain: A Personal Knowledge Base with Semantic Search

Open Brain is a personal, agent-readable knowledge base designed to capture, store, and retrieve your thoughts, notes, and ideas using a powerful semantic search backend. It uses a PostgreSQL database with the `pgvector` extension for efficient similarity search, a Telegram bot for easy knowledge capture, and a multi-transport Model Context Protocol (MCP) server to make your knowledge accessible to both local and remote AI agents.

This project provides a complete, production-ready implementation of the system described in the specification, including the database schema, capture and retrieval layers, and all necessary setup instructions.

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Features](#features)
- [Project Structure](#project-structure)
- [Setup and Installation](#setup-and-installation)
  - [1. Set Up Supabase](#1-set-up-supabase)
  - [2. Set Up Telegram Bot](#2-set-up-telegram-bot)
  - [3. Configure LLM Backend](#3-configure-llm-backend)
  - [4. Configure Environment](#4-configure-environment)
  - [5. Install Dependencies](#5-install-dependencies)
- [Usage](#usage)
  - [Running the Telegram Capture Layer](#running-the-telegram-capture-layer)
  - [Running the MCP Server](#running-the-mcp-server)
- [Configuration Reference](#configuration-reference)
- [Deployment Guidance](#deployment-guidance)

## Architecture Overview

The system is composed of three distinct layers that work together to provide a seamless knowledge management workflow.

| Layer | Technology | Purpose |
|---|---|---|
| **Database Layer** | Supabase (Postgres + pgvector) | Persistently stores memories, vector embeddings, and metadata. Provides high-performance cosine similarity search. |
| **Capture Layer** | Telegram Bot (python-telegram-bot) | Listens for messages sent to a Telegram bot, processes them with an LLM for cleaning and metadata extraction, and stores them in the database. |
| **Retrieval Layer** | MCP Server (FastMCP) | Exposes the knowledge base to AI agents via STDIO (for local tools like Cursor) or HTTP (for remote tools like Manus). |

## Features

- **Seamless Knowledge Capture**: Send any message to your Telegram bot to permanently store a memory.
- **AI-Powered Processing**: Uses a configurable LLM (OpenAI or Anthropic Claude) to automatically clean up raw text and extract structured metadata including tags, people, categories, and action items.
- **Dual LLM Backend**: Choose between OpenAI (e.g., `gpt-4.1-mini`) or Anthropic Claude (e.g., `claude-3-5-haiku-20241022`) for metadata extraction. Embeddings always use OpenAI for consistency.
- **Powerful Search and Write Tools**: Access your knowledge base with five MCP tools:
    - `semantic_search`: Find memories based on meaning and context.
    - `query_by_metadata`: Precisely filter by tags, people, or category.
    - `list_recent_memories`: Quickly retrieve your latest entries.
    - `thought_stats`: View aggregate statistics — total memories, date range, top tags, categories, and people.
    - `capture_thought`: Write a new memory directly from any MCP client (AI agents can now store memories, not just read them).
- **Dual-Transport MCP Server**: Run the server in different modes for maximum flexibility:
    - **STDIO**: For local, single-user desktop applications like Cursor and Claude Desktop.
    - **HTTP**: For remote, multi-client access from tools like Manus or custom cloud agents.
- **Cost-Efficient**: Built to run on the free tiers of Supabase and Telegram (free), with minimal API costs.

## Project Structure

```
/open-brain
├── migrations/
│   ├── 001_create_memories.sql   # Full schema for new deployments
│   └── 002_add_updated_at.sql    # Incremental migration for existing deployments
├── src/
│   ├── __init__.py               # Makes 'src' a Python package
│   ├── brain.py                  # Core logic (OpenAI/Anthropic/Supabase clients, DB ops)
│   ├── config.py                 # Environment variable loading and validation
│   ├── mcp_server.py             # The MCP server with dual-transport support
│   └── telegram_capture.py       # The Telegram bot capture layer
├── .env.example                  # Template for environment variables
├── README.md                     # This file
└── requirements.txt              # Python package dependencies
```

## Setup and Installation

Follow these steps to get your Open Brain system running.

### 1. Set Up Supabase

1. **Create a Supabase Project**: Go to [supabase.com](https://supabase.com), create a new project, and save your database password securely.
2. **Run the Migration Script**: Navigate to the **SQL Editor** in your Supabase project dashboard, create a **New Query**, paste the entire content of `migrations/001_create_memories.sql`, and click **RUN**. This creates the `memories` table (with `updated_at` column and auto-update trigger), HNSW index, GIN index, and both similarity search functions.
3. **Existing deployments**: If you already ran migration 001 before the `updated_at` column was added, also run `migrations/002_add_updated_at.sql` to add the column and trigger.
4. **Copy Credentials**: Go to **Settings → API** and copy your **Project URL** (`SUPABASE_URL`) and **service_role** key (`SUPABASE_SERVICE_KEY`).

### 2. Set Up Telegram Bot

1. Open Telegram and search for **@BotFather**.
2. Send `/newbot` and follow the prompts to choose a name and username for your bot.
3. BotFather will provide a token in the format `123456789:AAF...` — this is your `TELEGRAM_BOT_TOKEN`.
4. Optionally, send `/setdescription` and `/setcommands` to configure your bot's profile.

> **Tip**: To restrict who can send memories to your bot, find your Telegram user ID by messaging [@userinfobot](https://t.me/userinfobot) and set `TELEGRAM_ALLOWED_USER_IDS` in your `.env` file.

### 3. Configure LLM Backend

Open Brain supports two LLM backends for metadata extraction. Choose one:

#### Option A: OpenAI (default)

Set the following in your `.env`:

```env
LLM_BACKEND=openai
LLM_MODEL=gpt-4.1-mini
OPENAI_API_KEY=sk-proj-...
```

#### Option B: Anthropic Claude

Set the following in your `.env`:

```env
LLM_BACKEND=anthropic
LLM_MODEL=claude-3-5-haiku-20241022
ANTHROPIC_API_KEY=sk-ant-api03-...
```

Recommended Claude models by use case:

| Model | Speed | Cost | Best For |
|---|---|---|---|
| `claude-3-5-haiku-20241022` | Fastest | Lowest | High-volume capture, daily notes |
| `claude-3-5-sonnet-20241022` | Balanced | Medium | General use |
| `claude-opus-4-5` | Slowest | Highest | Complex documents, nuanced extraction |

> **Note**: Regardless of which LLM backend you choose, embeddings are always generated using the OpenAI API (`text-embedding-3-small`). Your `OPENAI_API_KEY` is always required.

### 4. Configure Environment

1. **Create `.env` file**: Copy `.env.example` to a new file named `.env`.
2. **Fill in Values**: Open `.env` and populate it with your credentials.

```bash
cp .env.example .env
# Edit .env with your credentials
```

### 5. Install Dependencies

This project uses `pip` for package management. It is highly recommended to use a virtual environment.

```bash
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Usage

### Running the Telegram Capture Layer

This process starts the Telegram bot and listens for incoming messages.

```bash
# Run from the project root
python -m src.telegram_capture
```

Once running, send any message to your bot on Telegram. You will receive a confirmation reply like:

```
✅ Memory captured!
📂 Category: idea
🏷 Tags: `open-brain`, `knowledge-base`
🎯 Action items: none
🆔 ID: d38e2d03-41c8-4a44-a9ec-2e7b2c122336
```

Available bot commands:
- `/start` — Welcome message and usage instructions
- `/status` — Confirm the bot is running and connected

### Running the MCP Server

The MCP server can be run in two primary modes: **STDIO** for local clients and **HTTP** for remote clients. Control the mode via the `MCP_TRANSPORT` environment variable or the `--transport` command-line flag.

#### Available MCP Tools

| Tool | Description |
|---|---|
| `semantic_search` | Search memories by meaning using cosine similarity |
| `list_recent_memories` | Return the N most recent memories |
| `query_by_metadata` | Filter by tags, people, or category |
| `thought_stats` | Aggregate statistics: total count, date range, top tags/categories/people |
| `capture_thought` | Write a new memory from any MCP client (AI agents can store memories) |

#### Mode 1: STDIO (for Local Clients)

This is the default mode, perfect for desktop applications like Cursor or Claude Desktop.

```bash
# Run in default STDIO mode
python -m src.mcp_server

# Or be explicit
python -m src.mcp_server --transport stdio
```

**Connecting to Cursor:**
1. Go to **Settings → MCP** in Cursor and click **Add Server**.
2. Choose **Command** and give it a name (e.g., "Open Brain").
3. For the command, provide the full path to your Python executable and the script:
    ```
    /path/to/your/open-brain/.venv/bin/python -m src.mcp_server --transport stdio
    ```

**Connecting to Claude Desktop:**

Add the following to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "open-brain": {
      "command": "/path/to/your/open-brain/.venv/bin/python",
      "args": ["-m", "src.mcp_server", "--transport", "stdio"],
      "cwd": "/path/to/your/open-brain"
    }
  }
}
```

#### Mode 2: HTTP (for Remote Clients)

This mode starts a web server, making your knowledge base accessible over the network to tools like Manus.

```bash
# Run in HTTP mode, listening on all network interfaces on port 8000
python -m src.mcp_server --transport http --host 0.0.0.0 --port 8000
```

**Connecting to Manus:**
1. Run the server in HTTP mode on a publicly accessible machine.
2. **Important**: For security, set the `MCP_AUTH_TOKEN` in your `.env` file. Generate one with:
   ```bash
   python3 -c "import secrets; print(secrets.token_hex(32))"
   ```
3. In Manus, add a new MCP server with:
    - **URL**: `http://<your-server-ip>:8000/mcp`
    - **Authentication**: `Bearer <your-auth-token>`

**Bearer Token Security:**
When `MCP_AUTH_TOKEN` is set, all HTTP/SSE requests must include:
```
Authorization: Bearer <your-token>
```
The `/health` endpoint is always accessible without authentication (for load-balancer health checks). Requests with a missing or incorrect token receive `HTTP 401`.

**Health check endpoint** (available in `http` and `sse` modes):
```
GET http://<host>:<port>/health
→ {"status": "healthy", "service": "open-brain-mcp"}
```

## Configuration Reference

All settings are configured via environment variables. See `.env.example` for the full annotated template.

| Variable | Required For | Description |
|---|---|---|
| `SUPABASE_URL` | Both | The URL of your Supabase project. |
| `SUPABASE_SERVICE_KEY` | Both | The `service_role` key for your Supabase project. |
| `OPENAI_API_KEY` | Both | Your OpenAI API key. Always required for embeddings. |
| `OPENAI_EMBEDDING_BASE_URL` | Both (Opt) | Base URL for the embeddings API. Defaults to `https://api.openai.com/v1`. |
| `EMBEDDING_MODEL` | Both (Opt) | Embedding model name. Defaults to `text-embedding-3-small`. |
| `EMBEDDING_BACKEND` | Both (Opt) | `openai` (default) or `local` (for testing without an API key). |
| `LLM_BACKEND` | Both (Opt) | LLM provider for metadata extraction: `openai` (default) or `anthropic`. |
| `LLM_MODEL` | Both (Opt) | Model name for metadata extraction. Defaults to `gpt-4.1-mini`. |
| `ANTHROPIC_API_KEY` | When `LLM_BACKEND=anthropic` | Your Anthropic API key. Required only for the Claude backend. |
| `TELEGRAM_BOT_TOKEN` | Telegram Capture | Bot token from @BotFather. |
| `TELEGRAM_ALLOWED_USER_IDS` | Telegram Capture (Opt) | Comma-separated list of allowed Telegram user IDs. Leave empty to allow all. |
| `MCP_TRANSPORT` | MCP Server (Opt) | Transport mode: `stdio` (default), `http`, or `sse`. |
| `MCP_HOST` | MCP Server (Opt) | Host to bind for `http`/`sse`. Use `0.0.0.0` for remote access. Defaults to `127.0.0.1`. |
| `MCP_PORT` | MCP Server (Opt) | Port for `http`/`sse` transport. Defaults to `8000`. |
| `MCP_AUTH_TOKEN` | MCP Server (Opt) | Bearer token to secure the `http`/`sse` endpoint. Strongly recommended for remote servers. |
| `LOG_LEVEL` | Both (Opt) | Logging level: `DEBUG`, `INFO`, `WARNING`. Defaults to `INFO`. |

## Deployment Guidance

### Deploying the Telegram Capture Layer

Run `src.telegram_capture` as a persistent background service using `systemd`:

```ini
# /etc/systemd/system/open-brain-telegram.service
[Unit]
Description=Open Brain Telegram Capture Layer
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/open-brain
EnvironmentFile=/home/ubuntu/open-brain/.env
ExecStart=/home/ubuntu/open-brain/.venv/bin/python -m src.telegram_capture
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable open-brain-telegram
sudo systemctl start open-brain-telegram
```

### Deploying the MCP Server (Remote / HTTP Mode)

```ini
# /etc/systemd/system/open-brain-mcp.service
[Unit]
Description=Open Brain MCP Server
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/open-brain
EnvironmentFile=/home/ubuntu/open-brain/.env
ExecStart=/home/ubuntu/open-brain/.venv/bin/python -m src.mcp_server --transport http --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### Connecting to ChatGPT (OAuth 2.0)

The MCP server includes a built-in OAuth 2.0 authorization server, enabling it to work as a **remote MCP connector** in ChatGPT. This follows the MCP specification's authorization flow (RFC 8414 + RFC 9728 + PKCE).

#### Prerequisites

1. The MCP server must be running in **HTTP mode** on a publicly accessible URL with HTTPS.
2. Set `OAUTH_USER_PASSWORD` in your `.env` file. This is the password you will enter once in the browser to authorize ChatGPT. Generate one with:
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
3. Optionally set `OAUTH_SERVER_URL` to your public HTTPS URL if the server cannot auto-detect it from request headers.

#### Step-by-Step: Register in ChatGPT

1. Go to [chatgpt.com](https://chatgpt.com) and open **Settings** (gear icon).
2. Navigate to the **MCP** or **Tools** section (may vary by ChatGPT version).
3. Click **Add MCP Server** (or "Add remote server").
4. Enter the **Server URL**: `https://<your-server-domain>/mcp`
5. ChatGPT will auto-discover the OAuth endpoints via `/.well-known/oauth-protected-resource` and `/.well-known/oauth-authorization-server`.
6. A browser window will open showing the **Open Brain authorization page**.
7. Enter your `OAUTH_USER_PASSWORD` and click **Authorize**.
8. ChatGPT will exchange the authorization code for an access token (PKCE-protected).
9. You are now connected. ChatGPT can use all five Open Brain tools.

#### OAuth Endpoints Reference

| Endpoint | Method | Purpose |
|---|---|---|
| `/.well-known/oauth-protected-resource` | GET | RFC 9728 Protected Resource Metadata |
| `/.well-known/oauth-authorization-server` | GET | RFC 8414 Authorization Server Metadata |
| `/authorize` | GET | Renders the consent/login form |
| `/authorize` | POST | Processes the form, issues authorization code |
| `/token` | POST | Exchanges authorization code for access token (PKCE) |

#### OAuth Configuration Variables

| Variable | Required | Description |
|---|---|---|
| `OAUTH_USER_PASSWORD` | Yes (for OAuth) | Password for the authorization consent form |
| `OAUTH_SERVER_URL` | Optional | Public base URL override (e.g., `https://brain.example.com`) |
| `OAUTH_TOKEN_EXPIRY` | Optional | Access token lifetime in seconds (default: `86400` = 24 hours) |

> **Note**: The existing `MCP_AUTH_TOKEN` bearer token authentication continues to work as a fallback for Manus, Cursor, and Claude Desktop. Both authentication methods are supported simultaneously.

### Security Checklist

- Set `MCP_AUTH_TOKEN` to a strong random secret for any internet-facing server.
- Set `OAUTH_USER_PASSWORD` to a strong random secret for ChatGPT OAuth access.
- Ensure your `.env` file is not committed to version control (it is listed in `.gitignore`).
- Use `TELEGRAM_ALLOWED_USER_IDS` to restrict bot access to your own Telegram account.
- Consider placing the MCP server behind a reverse proxy (nginx/Caddy) with TLS for HTTPS.
- OAuth access tokens are stored in-memory and expire after 24 hours by default. Server restarts will invalidate all tokens (ChatGPT will re-authorize automatically).
