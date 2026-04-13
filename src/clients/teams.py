"""
src/clients/teams.py

Microsoft Graph API client for Teams integration.

Auth strategy: OAuth 2.0 client_credentials flow (app-only auth).
Credentials come from the .env file:
    TENANT_ID           — Azure AD tenant ID
    TEAMS_CLIENT_ID     — App registration client ID
    TEAMS_CLIENT_VALUE  — App registration client secret value
    CHAT_ID             — Default chat thread ID (optional convenience)

Token is cached in memory and refreshed automatically when it expires.

Required Azure AD app permissions (Application, not Delegated):
    Chat.ReadWrite.All          — send/read chat messages
    ChannelMessage.Send         — send channel messages
    Presence.Read.All           — read user presence

NOTE: These credentials are configured in .env but were previously unused.
This is the first module that wires them up.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests


GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"


# ── Configuration ─────────────────────────────────────────────────────────────

@dataclass(slots=True)
class TeamsConfig:
    tenant_id: str
    client_id: str
    client_secret: str
    default_chat_id: Optional[str] = None
    request_timeout: int = 15

    @classmethod
    def from_env(cls) -> "TeamsConfig":
        return cls(
            tenant_id=(os.getenv("TENANT_ID") or "").strip(),
            client_id=(os.getenv("TEAMS_CLIENT_ID") or "").strip(),
            # TEAMS_CLIENT_VALUE holds the actual secret value created in Azure AD
            client_secret=(os.getenv("TEAMS_CLIENT_VALUE") or "").strip(),
            default_chat_id=(os.getenv("CHAT_ID") or "").strip() or None,
            request_timeout=int(os.getenv("TEAMS_REQUEST_TIMEOUT", "15")),
        )

    def validate(self) -> None:
        missing = [
            name
            for name, value in {
                "TENANT_ID":          self.tenant_id,
                "TEAMS_CLIENT_ID":    self.client_id,
                "TEAMS_CLIENT_VALUE": self.client_secret,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError(
                f"Missing required Teams environment variables: {', '.join(missing)}"
            )


# ── Client ────────────────────────────────────────────────────────────────────

class TeamsClient:
    """
    MS Graph client for Teams operations.

    Token is acquired via client_credentials and cached until 60 seconds
    before expiry to avoid hitting the API with an expired token mid-request.
    """

    def __init__(self, config: Optional[TeamsConfig] = None) -> None:
        self.config = config or TeamsConfig.from_env()
        self.config.validate()
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0
        self._session = requests.Session()

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _acquire_token(self) -> str:
        url = TOKEN_URL_TEMPLATE.format(tenant_id=self.config.tenant_id)
        resp = self._session.post(
            url,
            data={
                "grant_type":    "client_credentials",
                "client_id":     self.config.client_id,
                "client_secret": self.config.client_secret,
                "scope":         GRAPH_SCOPE,
            },
            timeout=self.config.request_timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        # expires_in is in seconds; cache with 60-second safety buffer
        self._token_expiry = time.time() + int(data.get("expires_in", 3600)) - 60
        return self._token

    def _get_token(self) -> str:
        if not self._token or time.time() >= self._token_expiry:
            self._acquire_token()
        return self._token  # type: ignore[return-value]

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type":  "application/json",
        }

    # ── Core HTTP ─────────────────────────────────────────────────────────────

    def _get(self, path: str, **kwargs: Any) -> Any:
        url = f"{GRAPH_BASE}/{path.lstrip('/')}"
        resp = self._session.get(
            url, headers=self._headers(), timeout=self.config.request_timeout, **kwargs
        )
        resp.raise_for_status()
        return resp.json() if resp.text.strip() else None

    def _post(self, path: str, payload: Dict[str, Any]) -> Any:
        url = f"{GRAPH_BASE}/{path.lstrip('/')}"
        resp = self._session.post(
            url,
            headers=self._headers(),
            json=payload,
            timeout=self.config.request_timeout,
        )
        resp.raise_for_status()
        return resp.json() if resp.text.strip() else None

    # ── Chat messages ─────────────────────────────────────────────────────────

    def send_message(self, text: str, *, chat_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Send a plain-text message to a chat thread.

        Uses config.default_chat_id when chat_id is not provided.
        Returns the created message object from the Graph API.
        """
        target_chat = chat_id or self.config.default_chat_id
        if not target_chat:
            raise ValueError(
                "chat_id must be provided or CHAT_ID must be set in .env"
            )
        return self._post(
            f"chats/{target_chat}/messages",
            {"body": {"contentType": "text", "content": text}},
        )

    def send_html_message(
        self, html: str, *, chat_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Send an HTML-formatted message to a chat thread."""
        target_chat = chat_id or self.config.default_chat_id
        if not target_chat:
            raise ValueError("chat_id must be provided or CHAT_ID must be set in .env")
        return self._post(
            f"chats/{target_chat}/messages",
            {"body": {"contentType": "html", "content": html}},
        )

    # ── Channel messages ──────────────────────────────────────────────────────

    def send_channel_message(
        self,
        team_id: str,
        channel_id: str,
        text: str,
        *,
        html: bool = False,
    ) -> Dict[str, Any]:
        """
        Post a message to a Teams channel.

        team_id and channel_id are Graph API identifiers (not display names).
        Set html=True to send HTML-formatted content.
        """
        content_type = "html" if html else "text"
        return self._post(
            f"teams/{team_id}/channels/{channel_id}/messages",
            {"body": {"contentType": content_type, "content": text}},
        )

    # ── Presence ──────────────────────────────────────────────────────────────

    def get_user_presence(self, user_id: str) -> Dict[str, Any]:
        """
        Get the current Teams presence for a user.

        Returns a presence object with keys:
            availability — e.g. "Available", "Busy", "Away", "BeRightBack",
                           "DoNotDisturb", "Offline", "PresenceUnknown"
            activity     — e.g. "Available", "InACall", "InAMeeting", etc.

        Requires Presence.Read.All application permission.
        """
        return self._get(f"users/{user_id}/presence")

    def get_users_presence(self, user_ids: List[str]) -> List[Dict[str, Any]]:
        """
        Batch presence lookup for multiple users (up to 650 per call).

        More efficient than calling get_user_presence() in a loop.
        Requires Presence.Read.All application permission.
        """
        result = self._post(
            "communications/getPresencesByUserId",
            {"ids": user_ids},
        )
        return result.get("value", []) if isinstance(result, dict) else []

    # ── User lookup ───────────────────────────────────────────────────────────

    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """Look up a Graph user object by their email / UPN."""
        result = self._get(
            "users",
            params={"$filter": f"mail eq '{email}' or userPrincipalName eq '{email}'"},
        )
        users = result.get("value", []) if isinstance(result, dict) else []
        return users[0] if users else None
