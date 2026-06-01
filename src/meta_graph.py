"""Minimal Meta Graph client for approved digital-twin reply sends."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass

import httpx


class MetaGraphError(RuntimeError):
    """Raised when the Meta Graph API rejects a send request."""


@dataclass
class MetaGraphClient:
    access_token: str
    instagram_actor_id: str
    api_version: str = "v22.0"
    base_url: str = "https://graph.instagram.com"
    app_secret: str = ""
    timeout_s: float = 30.0

    def _appsecret_proof(self) -> str | None:
        if not self.app_secret:
            return None
        return hmac.new(
            self.app_secret.encode("utf-8"),
            self.access_token.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _message_url(self) -> str:
        return (
            f"{self.base_url.rstrip('/')}/"
            f"{self.api_version.strip('/')}/"
            f"{self.instagram_actor_id}/messages"
        )

    async def send_text_message(
        self,
        *,
        recipient_id: str,
        text: str,
        reply_to_message_id: str | None = None,
    ) -> dict:
        """Send one text message through the Meta Graph API."""
        if not self.access_token:
            raise MetaGraphError("META_GRAPH_ACCESS_TOKEN is not configured.")
        if not self.instagram_actor_id:
            raise MetaGraphError("META_GRAPH_IG_USER_ID is not configured.")

        body = {
            "recipient": {"id": recipient_id},
            "message": {"text": text},
        }
        if reply_to_message_id:
            body["reply_to"] = {"mid": reply_to_message_id}

        params = {"access_token": self.access_token}
        appsecret_proof = self._appsecret_proof()
        if appsecret_proof:
            params["appsecret_proof"] = appsecret_proof

        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            response = await client.post(
                self._message_url(),
                params=params,
                json=body,
                headers={"content-type": "application/json"},
            )

        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise MetaGraphError(
                f"Meta Graph returned non-JSON response with HTTP {response.status_code}."
            ) from exc

        if response.status_code >= 400:
            error = payload.get("error") or {}
            message = error.get("message") or payload
            raise MetaGraphError(f"Meta Graph send failed: {message}")

        return payload
