"""Minimal Yahoo OAuth2 helpers (stdlib only).

We handle the code exchange and refresh ourselves so the registered redirect URI
(https://localhost:8080) is always used, then hand a fresh token dict to yfpy.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
DEFAULT_REDIRECT = "https://localhost:8080"


def redirect_uri() -> str:
    return os.environ.get("YAHOO_REDIRECT_URI", DEFAULT_REDIRECT)


def authorize_url(client_id: str) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "language": "en-us",
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"


class YahooAuthError(RuntimeError):
    def __init__(self, status: int, error: str, description: str):
        self.status, self.error, self.description = status, error, description
        super().__init__(f"Yahoo token request failed (HTTP {status}): {error} — {description}")


def _token_request(client_id: str, client_secret: str, data: dict) -> dict:
    client_id, client_secret = client_id.strip(), client_secret.strip()
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    body = urllib.parse.urlencode(
        {
            **data,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri(),
        }
    ).encode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            err = json.loads(raw)
        except ValueError:
            err = {"error": exc.reason, "error_description": raw[:300]}
        raise YahooAuthError(exc.code, str(err.get("error", "")), str(err.get("error_description", ""))) from None
    payload["token_time"] = time.time()
    return payload


def exchange_code(client_id: str, client_secret: str, code: str) -> dict:
    return _token_request(client_id, client_secret, {"grant_type": "authorization_code", "code": code})


def refresh(client_id: str, client_secret: str, refresh_token: str) -> dict:
    return _token_request(
        client_id, client_secret, {"grant_type": "refresh_token", "refresh_token": refresh_token}
    )


def yfpy_token_dict(client_id: str, client_secret: str, token: dict) -> dict:
    """Shape a token response into the dict yfpy expects for yahoo_access_token_json."""
    return {
        "access_token": token["access_token"],
        "consumer_key": client_id,
        "consumer_secret": client_secret,
        "guid": token.get("xoauth_yahoo_guid"),
        "refresh_token": token["refresh_token"],
        "token_time": token["token_time"],
        "token_type": token.get("token_type", "bearer"),
    }
