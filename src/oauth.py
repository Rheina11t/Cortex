"""
Open Brain – OAuth 2.0 Authorization Server.

Implements the MCP-spec-compliant OAuth 2.0 flow so that ChatGPT (and any
other MCP client that speaks OAuth) can authenticate against this server.

Endpoints provided:
  /.well-known/oauth-protected-resource   – RFC 9728 Protected Resource Metadata
  /.well-known/oauth-authorization-server  – RFC 8414 Authorization Server Metadata
  /authorize                               – Authorization endpoint (shows consent form)
  /token                                   – Token endpoint (code → access-token exchange)

Design notes:
  • This is a *personal, single-user* server.  "Authorization" means entering
    the owner's password (OAUTH_USER_PASSWORD env var).
  • Auth codes, PKCE verifiers, and access tokens are stored in-memory.
  • Access tokens are long-lived (configurable, default 24 h).
  • The existing MCP_AUTH_TOKEN bearer-token auth is kept as a fallback so
    that Manus, Cursor, and Claude Desktop continue to work unchanged.
"""

from __future__ import annotations

import hashlib
import html
import logging
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

logger = logging.getLogger("open_brain.oauth")

# ---------------------------------------------------------------------------
# In-memory stores
# ---------------------------------------------------------------------------

@dataclass
class AuthCode:
    """Represents a pending authorization code."""
    code: str
    client_id: str
    redirect_uri: str
    scope: str
    code_challenge: str
    code_challenge_method: str
    created_at: float = field(default_factory=time.time)
    used: bool = False

    def is_expired(self, ttl: int = 600) -> bool:
        return (time.time() - self.created_at) > ttl


@dataclass
class AccessToken:
    """Represents an issued access token."""
    token: str
    client_id: str
    scope: str
    created_at: float = field(default_factory=time.time)
    expires_in: int = 86400  # 24 hours

    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > self.expires_in


class OAuthStore:
    """Simple in-memory store for auth codes and access tokens."""

    def __init__(self) -> None:
        self._auth_codes: dict[str, AuthCode] = {}
        self._access_tokens: dict[str, AccessToken] = {}

    def create_auth_code(
        self,
        client_id: str,
        redirect_uri: str,
        scope: str,
        code_challenge: str,
        code_challenge_method: str,
    ) -> str:
        code = secrets.token_urlsafe(48)
        self._auth_codes[code] = AuthCode(
            code=code,
            client_id=client_id,
            redirect_uri=redirect_uri,
            scope=scope,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
        )
        self._cleanup()
        return code

    def consume_auth_code(self, code: str) -> Optional[AuthCode]:
        ac = self._auth_codes.get(code)
        if ac is None or ac.used or ac.is_expired():
            return None
        ac.used = True
        return ac

    def create_access_token(
        self,
        client_id: str,
        scope: str,
        expires_in: int = 86400,
    ) -> AccessToken:
        token_str = secrets.token_urlsafe(64)
        tok = AccessToken(
            token=token_str,
            client_id=client_id,
            scope=scope,
            expires_in=expires_in,
        )
        self._access_tokens[token_str] = tok
        self._cleanup()
        return tok

    def validate_access_token(self, token: str) -> Optional[AccessToken]:
        tok = self._access_tokens.get(token)
        if tok is None or tok.is_expired():
            return None
        return tok

    def _cleanup(self) -> None:
        """Remove expired entries to prevent unbounded growth."""
        now = time.time()
        self._auth_codes = {
            k: v for k, v in self._auth_codes.items()
            if not v.is_expired() and not v.used
        }
        self._access_tokens = {
            k: v for k, v in self._access_tokens.items()
            if not v.is_expired()
        }


# Global store instance
store = OAuthStore()


# ---------------------------------------------------------------------------
# PKCE verification
# ---------------------------------------------------------------------------

def verify_pkce(code_verifier: str, code_challenge: str, method: str) -> bool:
    """Verify PKCE code_verifier against code_challenge."""
    if method == "S256":
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        # Base64url encode without padding
        import base64
        computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        return computed == code_challenge
    elif method == "plain":
        return code_verifier == code_challenge
    return False


# ---------------------------------------------------------------------------
# OAuth route handlers
# ---------------------------------------------------------------------------

def get_server_base_url(request: Request, settings) -> str:
    """Derive the public base URL of this server from the request or config."""
    # If OAUTH_SERVER_URL is explicitly set, use it
    if hasattr(settings, "oauth_server_url") and settings.oauth_server_url:
        return settings.oauth_server_url.rstrip("/")
    # Otherwise derive from the incoming request
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", "localhost"))
    return f"{scheme}://{host}"


async def protected_resource_metadata(request: Request, settings) -> JSONResponse:
    """
    GET /.well-known/oauth-protected-resource

    RFC 9728 – tells MCP clients where the authorization server lives.
    """
    base_url = get_server_base_url(request, settings)
    return JSONResponse({
        "resource": base_url,
        "authorization_servers": [base_url],
        "scopes_supported": ["mcp:tools"],
        "bearer_methods_supported": ["header"],
    })


async def authorization_server_metadata(request: Request, settings) -> JSONResponse:
    """
    GET /.well-known/oauth-authorization-server

    RFC 8414 – tells MCP clients about the OAuth endpoints and capabilities.
    """
    base_url = get_server_base_url(request, settings)
    return JSONResponse({
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/authorize",
        "token_endpoint": f"{base_url}/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256", "plain"],
        "scopes_supported": ["mcp:tools"],
        "token_endpoint_auth_methods_supported": ["none"],
        "client_id_metadata_document_supported": True,
    })


async def authorize_get(request: Request, settings) -> Response:
    """
    GET /authorize

    Shows a simple HTML consent/login form.  The user enters the
    OAUTH_USER_PASSWORD to approve the authorization request.
    """
    # Extract OAuth parameters
    params = request.query_params
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    state = params.get("state", "")
    scope = params.get("scope", "mcp:tools")
    code_challenge = params.get("code_challenge", "")
    code_challenge_method = params.get("code_challenge_method", "S256")
    response_type = params.get("response_type", "")

    if response_type != "code":
        return HTMLResponse(
            "<h1>Error</h1><p>Only response_type=code is supported.</p>",
            status_code=400,
        )

    # Render a simple login form
    form_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Open Brain – Authorize</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0f172a;
            color: #e2e8f0;
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
        }}
        .card {{
            background: #1e293b;
            border-radius: 12px;
            padding: 2rem;
            max-width: 420px;
            width: 90%;
            box-shadow: 0 20px 60px rgba(0,0,0,0.5);
        }}
        h1 {{
            font-size: 1.5rem;
            margin-bottom: 0.5rem;
            color: #38bdf8;
        }}
        .subtitle {{
            color: #94a3b8;
            font-size: 0.9rem;
            margin-bottom: 1.5rem;
        }}
        .info {{
            background: #0f172a;
            border-radius: 8px;
            padding: 1rem;
            margin-bottom: 1.5rem;
            font-size: 0.85rem;
        }}
        .info dt {{ color: #94a3b8; }}
        .info dd {{ color: #e2e8f0; margin-bottom: 0.5rem; word-break: break-all; }}
        label {{
            display: block;
            font-size: 0.85rem;
            color: #94a3b8;
            margin-bottom: 0.3rem;
        }}
        input[type="password"] {{
            width: 100%;
            padding: 0.7rem;
            border: 1px solid #334155;
            border-radius: 6px;
            background: #0f172a;
            color: #e2e8f0;
            font-size: 1rem;
            margin-bottom: 1rem;
        }}
        input[type="password"]:focus {{
            outline: none;
            border-color: #38bdf8;
        }}
        .buttons {{
            display: flex;
            gap: 0.75rem;
        }}
        button {{
            flex: 1;
            padding: 0.7rem;
            border: none;
            border-radius: 6px;
            font-size: 1rem;
            cursor: pointer;
            font-weight: 600;
        }}
        .btn-approve {{
            background: #38bdf8;
            color: #0f172a;
        }}
        .btn-approve:hover {{ background: #7dd3fc; }}
        .btn-deny {{
            background: #334155;
            color: #94a3b8;
        }}
        .btn-deny:hover {{ background: #475569; }}
        .error {{
            background: #7f1d1d;
            color: #fca5a5;
            padding: 0.7rem;
            border-radius: 6px;
            margin-bottom: 1rem;
            font-size: 0.85rem;
            display: none;
        }}
    </style>
</head>
<body>
    <div class="card">
        <h1>Open Brain</h1>
        <p class="subtitle">An application is requesting access to your knowledge base.</p>
        <div class="info">
            <dl>
                <dt>Client ID</dt>
                <dd>{html.escape(client_id[:80])}</dd>
                <dt>Scope</dt>
                <dd>{html.escape(scope)}</dd>
            </dl>
        </div>
        <div class="error" id="error-msg"></div>
        <form method="POST" action="/authorize">
            <input type="hidden" name="client_id" value="{html.escape(client_id)}">
            <input type="hidden" name="redirect_uri" value="{html.escape(redirect_uri)}">
            <input type="hidden" name="state" value="{html.escape(state)}">
            <input type="hidden" name="scope" value="{html.escape(scope)}">
            <input type="hidden" name="code_challenge" value="{html.escape(code_challenge)}">
            <input type="hidden" name="code_challenge_method" value="{html.escape(code_challenge_method)}">
            <label for="password">Enter your Open Brain password to authorize:</label>
            <input type="password" id="password" name="password" placeholder="Password" required autofocus>
            <div class="buttons">
                <button type="submit" name="action" value="approve" class="btn-approve">Authorize</button>
                <button type="submit" name="action" value="deny" class="btn-deny">Deny</button>
            </div>
        </form>
    </div>
</body>
</html>"""
    return HTMLResponse(form_html)


async def authorize_post(request: Request, settings) -> Response:
    """
    POST /authorize

    Processes the consent form.  If the password matches, issues an
    authorization code and redirects back to the client.
    """
    form = await request.form()
    action = form.get("action", "approve" if form.get("password") else "deny")
    client_id = str(form.get("client_id", ""))
    redirect_uri = str(form.get("redirect_uri", ""))
    state = str(form.get("state", ""))
    scope = str(form.get("scope", "mcp:tools"))
    code_challenge = str(form.get("code_challenge", ""))
    code_challenge_method = str(form.get("code_challenge_method", "S256"))
    password = str(form.get("password", ""))

    if action == "deny":
        # User denied – redirect with error
        sep = "&" if "?" in redirect_uri else "?"
        deny_url = f"{redirect_uri}{sep}error=access_denied&state={urllib.parse.quote(state)}"
        return RedirectResponse(url=deny_url, status_code=302)

    # Validate password
    expected_password = settings.oauth_user_password
    if not expected_password:
        logger.error("OAUTH_USER_PASSWORD is not set – cannot authorize")
        return HTMLResponse(
            "<h1>Server Error</h1><p>OAUTH_USER_PASSWORD is not configured.</p>",
            status_code=500,
        )

    if password != expected_password:
        logger.warning("OAuth authorization failed – wrong password from %s",
                       request.client.host if request.client else "unknown")
        # Re-render the form with an error (simple redirect back with error flag)
        # For simplicity, redirect back to GET /authorize with an error param
        original_params = urllib.parse.urlencode({
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": scope,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "error": "invalid_password",
        })
        return RedirectResponse(url=f"/authorize?{original_params}", status_code=302)

    # Password correct – issue authorization code
    code = store.create_auth_code(
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=scope,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
    )
    logger.info("Issued auth code for client_id=%s", client_id[:40])

    sep = "&" if "?" in redirect_uri else "?"
    redirect_url = f"{redirect_uri}{sep}code={urllib.parse.quote(code)}&state={urllib.parse.quote(state)}"
    return RedirectResponse(url=redirect_url, status_code=302)


async def token_endpoint(request: Request, settings) -> Response:
    """
    POST /token

    Exchanges an authorization code for an access token.
    Validates PKCE code_verifier against the stored code_challenge.
    """
    # Parse form-encoded body (standard for OAuth token requests)
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        data = dict(form)
    elif "application/json" in content_type:
        data = await request.json()
    else:
        # Try form-encoded as default
        try:
            form = await request.form()
            data = dict(form)
        except Exception:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "Unsupported content type"},
                status_code=400,
            )

    grant_type = data.get("grant_type", "")
    code = data.get("code", "")
    redirect_uri = data.get("redirect_uri", "")
    code_verifier = data.get("code_verifier", "")
    client_id = data.get("client_id", "")

    if grant_type != "authorization_code":
        return JSONResponse(
            {"error": "unsupported_grant_type", "error_description": "Only authorization_code is supported"},
            status_code=400,
        )

    if not code:
        return JSONResponse(
            {"error": "invalid_request", "error_description": "Missing code parameter"},
            status_code=400,
        )

    # Consume the auth code
    auth_code = store.consume_auth_code(code)
    if auth_code is None:
        return JSONResponse(
            {"error": "invalid_grant", "error_description": "Invalid, expired, or already-used authorization code"},
            status_code=400,
        )

    # Validate redirect_uri matches
    if redirect_uri and redirect_uri != auth_code.redirect_uri:
        return JSONResponse(
            {"error": "invalid_grant", "error_description": "redirect_uri mismatch"},
            status_code=400,
        )

    # Validate PKCE
    if auth_code.code_challenge:
        if not code_verifier:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "Missing code_verifier for PKCE"},
                status_code=400,
            )
        if not verify_pkce(code_verifier, auth_code.code_challenge, auth_code.code_challenge_method):
            logger.warning("PKCE verification failed for client_id=%s", client_id[:40])
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "PKCE verification failed"},
                status_code=400,
            )

    # Issue access token
    token_ttl = getattr(settings, "oauth_token_ttl", 86400)
    access_token = store.create_access_token(
        client_id=auth_code.client_id,
        scope=auth_code.scope,
        expires_in=token_ttl,
    )

    logger.info("Issued access token for client_id=%s (expires_in=%d)",
                auth_code.client_id[:40], token_ttl)

    return JSONResponse({
        "access_token": access_token.token,
        "token_type": "Bearer",
        "expires_in": access_token.expires_in,
        "scope": access_token.scope,
    })


def is_valid_oauth_token(token: str) -> bool:
    """Check if a bearer token is a valid OAuth access token."""
    return store.validate_access_token(token) is not None
