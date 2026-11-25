"""Anmeldung bei der Monsieur-Cuisine-Cloud (inoffiziell).

Ablauf (vollständig beschrieben in der Projektdatei anmeldung-und-live.md):
  1. Lidl-Login im Browser (OIDC Authorization Code + PKCE, mit reCAPTCHA) –
     NICHT automatisiert; der Nutzer meldet sich selbst an. authorize_url()
     baut den Link; die Rückleit-Adresse fügt der Nutzer im Einrichtungsdialog
     ein (parse_callback()).
  2. exchange_code():  POST /connect/token (grant_type=authorization_code) → Lidl-Tokens
  3. app_login():      POST /api/v2/users/login (verschlüsselt) → Tecpal-JWT
  4. refresh_jwt():    POST /api/v1/token/refresh → neues JWT (unverschlüsselt)

Die eigentliche Ver-/Entschlüsselung (Schritt 3) steckt in crypto.py.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import urllib.parse
import uuid
from dataclasses import dataclass

import aiohttp

from .crypto import decrypt_login_response, encrypt_login_payload

API_BASE = "https://mc-api.tecpal.com"
ACCOUNTS = "https://accounts.lidl.com"
CLIENT_ID = "monsieurcuisineappcompanionclient"
# Public client constant of the Android app, as published in monsieur-cuisine-cli.
CLIENT_SECRET = "12F42561-7F71-43D7-964A-4B2B1E2B19C3"
REDIRECT_URI = "lidl-com.monsieur-cuisine.companion://login/prod"
SCOPE = "openid profile offline_access"
KEY_ID = "351756051523999"


class McAuthError(Exception):
    pass


class CallbackError(ValueError):
    """The pasted callback address is unusable; the text is the form error key."""


@dataclass
class PkcePair:
    verifier: str
    challenge: str
    state: str
    nonce: str


def new_pkce() -> PkcePair:
    """Erzeugt Verifier + S256-Challenge für den Lidl-Login."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return PkcePair(verifier, challenge,
                    secrets.token_urlsafe(32), secrets.token_urlsafe(32))


def authorize_url(pkce: PkcePair, locale: str = "de-DE") -> str:
    """Browser-Link für den Lidl-Login (Nutzer löst dort auch das Captcha)."""
    q = {
        "prompt": "login", "response_type": "code", "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI, "scope": SCOPE,
        "code_challenge": pkce.challenge, "code_challenge_method": "S256",
        "state": pkce.state, "nonce": pkce.nonce,
        # Verified for Poland (2026-11-25) instead of the app's CountryCode/Language.
        "Country": locale.split("-")[-1].upper(), "language": locale.upper(),
    }
    return f"{ACCOUNTS}/connect/authorize?" + urllib.parse.urlencode(q)


def parse_callback(pasted: str, state: str) -> str:
    """The one-time code from the pasted ``lidl-com…://login/prod?…`` address.

    Accepts text around the address, because Chrome only shows it inside a
    console message.
    """
    match = re.search(re.escape(REDIRECT_URI) + r"/?\?[^\s'\"<>]+", pasted, re.IGNORECASE)
    if not match:
        raise CallbackError("invalid_callback")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(match.group()).query)
    if not secrets.compare_digest(query.get("state", [""])[0], state):
        raise CallbackError("state_mismatch")
    if not (code := query.get("code", [""])[0]):
        raise CallbackError("invalid_callback")
    return code


def _check_nonce(id_token: str, nonce: str) -> None:
    try:
        part = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        ok = secrets.compare_digest(claims["nonce"], nonce)
    except (IndexError, KeyError, TypeError, ValueError):
        ok = False
    if not ok:
        raise McAuthError("ID token nonce does not match")


async def exchange_code(session: aiohttp.ClientSession, code: str, pkce: PkcePair) -> dict:
    """Schritt 2: Authorization Code → Lidl access_token/id_token/refresh_token."""
    data = {"grant_type": "authorization_code", "code": code, "code_verifier": pkce.verifier,
            "redirect_uri": REDIRECT_URI, "client_id": CLIENT_ID}
    async with session.post(f"{ACCOUNTS}/connect/token", data=data,
                            auth=aiohttp.BasicAuth(CLIENT_ID, CLIENT_SECRET)) as resp:
        body = await resp.json(content_type=None)
    if resp.status != 200 or "access_token" not in body or "id_token" not in body:
        raise McAuthError(f"Code-Einlösung fehlgeschlagen: {resp.status} {body.get('error')}")
    _check_nonce(body["id_token"], pkce.nonce)
    return body


async def server_public_key(session: aiohttp.ClientSession) -> str:
    """Öffentlichen RSA-Schlüssel des Servers holen (Base64, ohne Anmeldung)."""
    async with session.get(f"{API_BASE}/api/v1/authkey/server/request-public-key") as resp:
        body = await resp.json(content_type=None)
    key = (body.get("data") or {}).get("serverPublicKey")
    if not key:
        raise McAuthError("Kein serverPublicKey erhalten")
    return key


async def app_login(session: aiohttp.ClientSession, access_token: str, id_token: str,
                    companion_app_id: str, locale: str = "de-DE") -> dict:
    """Schritt 3: verschlüsselter Login → Tecpal-Sitzung.

    Gibt das entschlüsselte `data`-Objekt zurück: { accessToken (= JWT), user{…}, … }.
    Die Ver-/Entschlüsselung kommt aus crypto.py.
    """
    public_key = await server_public_key(session)
    body = json.dumps({"accessToken": access_token, "idToken": id_token,
                       "clientId": CLIENT_ID, "companionAppId": companion_app_id})
    payload_b64, iv_b64, x_app_aes, key = encrypt_login_payload(body.encode(), public_key)
    headers = {"content-type": "application/json", "accept": "application/json",
               "x-app-aes": x_app_aes, "x-key-id": KEY_ID, "x-encrypt-method": "AES",
               "x-request-id": str(uuid.uuid4()), "device-type": "iOS",
               "appminorversion": "14204", "accept-language": locale}
    async with session.post(f"{API_BASE}/api/v2/users/login",
                            json={"payload": payload_b64, "iv": iv_b64}, headers=headers) as resp:
        enc = await resp.json(content_type=None)
    inner = enc.get("data", enc) if isinstance(enc, dict) else enc
    if not isinstance(inner, dict) or "payload" not in inner:
        raise McAuthError("App-Login fehlgeschlagen: unerwartete Antwort")
    clear = json.loads(decrypt_login_response(inner["payload"], inner["iv"], key))
    data = clear.get("data", clear)
    if not isinstance(data, dict) or not data.get("accessToken"):
        raise McAuthError(f"Login-Antwort ohne accessToken: {clear.get('message')}")
    return data


async def login_with_code(session: aiohttp.ClientSession, code: str, pkce: PkcePair,
                          locale: str = "de-DE") -> dict:
    """Kompletter Login ab dem `code` aus der Browser-Rückleitung.

    Gibt zurück: { jwt, user, companion_app_id }.
    """
    tok = await exchange_code(session, code, pkce)
    companion_app_id = new_companion_app_id()
    data = await app_login(session, tok["access_token"], tok["id_token"], companion_app_id, locale)
    return {
        "jwt": data["accessToken"],
        "user": data.get("user", {}),
        "companion_app_id": companion_app_id,
    }


async def refresh_jwt(session: aiohttp.ClientSession, jwt: str) -> str:
    """Schritt 4: Tecpal-JWT verlängern (unverschlüsselt)."""
    headers = {"authorization": f"Bearer {jwt}", "content-type": "application/json",
               "x-request-id": str(uuid.uuid4())}
    async with session.post(f"{API_BASE}/api/v1/token/refresh",
                            json={"jwt": jwt}, headers=headers) as resp:
        body = await resp.json(content_type=None)
    new = body.get("jwt") or (body.get("data") or {}).get("jwt")
    if resp.status != 200 or not new:
        raise McAuthError(f"JWT-Refresh fehlgeschlagen: {resp.status} code={body.get('code')}")
    return new


def new_companion_app_id() -> str:
    return str(uuid.uuid4())
