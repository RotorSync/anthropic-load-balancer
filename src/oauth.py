"""
OAuth token refresh and authorization for Anthropic API subscriptions.
"""
import asyncio
import base64
import hashlib
import logging
import os
import time
from typing import Optional, TYPE_CHECKING
from urllib.parse import urlencode

import httpx

from .config import SubscriptionConfig, save_config

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger(__name__)

OAUTH_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
OAUTH_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
OAUTH_SCOPE = "org:create_api_key user:profile user:inference"

# Browser-like headers required for token exchange
BROWSER_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://claude.ai/",
    "Origin": "https://claude.ai",
}

# Refresh tokens this many seconds before they expire
PROACTIVE_REFRESH_MARGIN = 300  # 5 minutes before expiry

# Minimum interval between refresh attempts for same subscription
MIN_REFRESH_INTERVAL = 30  # seconds


def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256)."""
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class OAuthManager:
    """
    Manages OAuth token lifecycle for subscriptions.

    - Authorization flow: generate auth URL, exchange code for tokens
    - Proactive refresh: background task refreshes tokens before expiry
    - Reactive refresh: on-demand refresh after 401 from Anthropic
    - Persists updated tokens to config.json atomically
    - Per-subscription locks prevent concurrent refresh races
    """

    def __init__(self, config: "Config", config_path: str):
        self._config = config
        self._config_path = config_path
        self._client: Optional[httpx.AsyncClient] = None
        self._last_refresh_attempt: dict[str, float] = {}
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        # Pending authorization flows: sub_name -> {verifier, state}
        self._pending_auth: dict[str, dict] = {}

    async def startup(self):
        """Initialize HTTP client."""
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))

    async def shutdown(self):
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()

    def _get_lock(self, name: str) -> asyncio.Lock:
        if name not in self._refresh_locks:
            self._refresh_locks[name] = asyncio.Lock()
        return self._refresh_locks[name]

    # ========================================================================
    # Authorization flow (initial token acquisition)
    # ========================================================================

    def start_auth(self, sub_name: str) -> str:
        """
        Start an OAuth authorization flow for a subscription.

        Returns the authorization URL the user should open in their browser.
        The PKCE verifier is stored internally for the exchange step.
        """
        verifier, challenge = _generate_pkce()
        state = os.urandom(32).hex()
        self._pending_auth[sub_name] = {"verifier": verifier, "state": state}

        params = {
            "code": "true",
            "client_id": OAUTH_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": OAUTH_REDIRECT_URI,
            "scope": OAUTH_SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        return f"{OAUTH_AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange_code(self, sub_name: str, code: str) -> dict:
        """
        Exchange an authorization code for access + refresh tokens.

        Args:
            sub_name: The subscription name to store tokens for.
            code: The authorization code from the callback URL.

        Returns:
            {"success": True, "expires_in": int} on success
            {"success": False, "error": str} on failure
        """
        pending = self._pending_auth.pop(sub_name, None)
        if not pending:
            return {"success": False, "error": f"No pending auth flow for '{sub_name}'. Call /admin/oauth/start first."}

        verifier = pending["verifier"]
        state = pending["state"]

        # Clean the code — may contain #state or &extra params
        auth_code = code.split("#")[0].split("&")[0]

        try:
            # Send as JSON with browser headers (matching claude-code-login)
            response = await self._client.post(
                OAUTH_TOKEN_URL,
                json={
                    "grant_type": "authorization_code",
                    "client_id": OAUTH_CLIENT_ID,
                    "code": auth_code,
                    "redirect_uri": OAUTH_REDIRECT_URI,
                    "code_verifier": verifier,
                    "state": state,
                },
                headers=BROWSER_HEADERS,
            )

            if response.status_code != 200:
                return {"success": False, "error": f"Token exchange failed: {response.status_code} {response.text}"}

            data = response.json()
            access_token = data.get("access_token")
            refresh_token = data.get("refresh_token")
            expires_in = data.get("expires_in", 3600)

            if not access_token or not refresh_token:
                return {"success": False, "error": f"Missing tokens in response: {list(data.keys())}"}

            # Find the subscription config and update it
            sub_config = None
            for sub in self._config.subscriptions:
                if sub.name == sub_name:
                    sub_config = sub
                    break

            if not sub_config:
                return {"success": False, "error": f"Subscription '{sub_name}' not found in config"}

            sub_config.api_key = access_token
            sub_config.refresh_token = refresh_token
            sub_config.token_expires_at = time.time() + expires_in

            # Persist to disk
            save_config(self._config, self._config_path)

            logger.info(f"{sub_name}: OAuth authorization complete, token expires in {expires_in}s")
            return {"success": True, "expires_in": expires_in}

        except Exception as e:
            logger.error(f"{sub_name}: OAuth code exchange error: {e}")
            return {"success": False, "error": str(e)}

    # ========================================================================
    # Token refresh (automatic)
    # ========================================================================

    async def refresh_token(self, sub_config: SubscriptionConfig) -> bool:
        """
        Refresh a single subscription's OAuth token.

        Returns True if refresh succeeded, False otherwise.
        Safe to call concurrently — per-subscription lock serializes attempts.
        """
        if not sub_config.is_oauth:
            return False

        lock = self._get_lock(sub_config.name)
        async with lock:
            now = time.time()
            last_attempt = self._last_refresh_attempt.get(sub_config.name, 0)

            # If we just refreshed and the token is valid, skip
            if now - last_attempt < MIN_REFRESH_INTERVAL:
                if sub_config.token_expires_at and sub_config.token_expires_at > now + 60:
                    logger.debug(f"{sub_config.name}: token already refreshed recently")
                    return True
                logger.debug(f"{sub_config.name}: skipping refresh, too soon since last attempt")
                return False

            self._last_refresh_attempt[sub_config.name] = now

            try:
                # Send refresh as JSON with browser headers
                response = await self._client.post(
                    OAUTH_TOKEN_URL,
                    json={
                        "grant_type": "refresh_token",
                        "client_id": OAUTH_CLIENT_ID,
                        "refresh_token": sub_config.refresh_token,
                    },
                    headers=BROWSER_HEADERS,
                )

                if response.status_code != 200:
                    logger.error(
                        f"{sub_config.name}: OAuth refresh failed: "
                        f"{response.status_code} {response.text}"
                    )
                    return False

                data = response.json()
                new_access = data["access_token"]
                new_refresh = data.get("refresh_token", sub_config.refresh_token)
                expires_in = data.get("expires_in", 3600)

                # Update config in-place
                sub_config.api_key = new_access
                sub_config.refresh_token = new_refresh
                sub_config.token_expires_at = time.time() + expires_in

                # Persist to disk
                try:
                    save_config(self._config, self._config_path)
                except Exception as e:
                    logger.error(f"{sub_config.name}: failed to persist config: {e}")

                logger.info(
                    f"{sub_config.name}: OAuth token refreshed, expires in {expires_in}s"
                )
                return True

            except Exception as e:
                logger.error(f"{sub_config.name}: OAuth refresh error: {e}")
                return False

    async def proactive_refresh_pass(self):
        """
        Check all OAuth subscriptions and refresh any near expiry.
        Called periodically from the background task.
        """
        now = time.time()
        for sub in self._config.subscriptions:
            if not sub.is_oauth:
                continue

            expires_at = sub.token_expires_at or 0
            remaining = expires_at - now

            if remaining < PROACTIVE_REFRESH_MARGIN:
                logger.info(
                    f"{sub.name}: token expires in {int(remaining)}s, refreshing proactively"
                )
                await self.refresh_token(sub)

    def needs_refresh(self, sub_config: SubscriptionConfig) -> bool:
        """Check if a subscription's token is expired or near expiry."""
        if not sub_config.is_oauth:
            return False
        expires_at = sub_config.token_expires_at or 0
        return time.time() > expires_at - PROACTIVE_REFRESH_MARGIN
