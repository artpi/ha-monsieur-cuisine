"""Sign-in link and the pasted callback address."""
import base64
import json
import sys
import urllib.parse
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1] / "custom_components" / "monsieur_cuisine"))

from mcbridge.auth import (  # noqa: E402
    REDIRECT_URI,
    CallbackError,
    McAuthError,
    _check_nonce,
    authorize_url,
    new_pkce,
    parse_callback,
)


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(url).query).items()}


def test_redirect_uri_is_the_registered_one():
    assert _query(authorize_url(new_pkce()))["redirect_uri"] == REDIRECT_URI
    assert REDIRECT_URI == "lidl-com.monsieur-cuisine.companion://login/prod"


def test_country_and_language():
    q = _query(authorize_url(new_pkce(), "pl-PL"))
    assert (q["Country"], q["language"]) == ("PL", "PL-PL")


def test_callback_inside_a_console_message():
    text = f"Failed to launch '{REDIRECT_URI}?code=ABC-1&state=S&session_state=x' because the scheme"
    assert parse_callback(text, "S") == "ABC-1"
    assert parse_callback(f"  {REDIRECT_URI}?code=ABC&state=S\n", "S") == "ABC"


@pytest.mark.parametrize(("text", "error"), [
    ("https://example.com/?code=A&state=S", "invalid_callback"),
    (f"{REDIRECT_URI}?state=S", "invalid_callback"),
    (f"{REDIRECT_URI}?code=A&state=other", "state_mismatch"),
    (f"{REDIRECT_URI}?code=A", "state_mismatch"),
])
def test_unusable_callbacks(text, error):
    with pytest.raises(CallbackError, match=error):
        parse_callback(text, "S")


def test_nonce():
    part = base64.urlsafe_b64encode(json.dumps({"nonce": "N"}).encode()).decode().rstrip("=")
    _check_nonce(f"h.{part}.s", "N")
    for token, nonce in ((f"h.{part}.s", "other"), ("broken", "N")):
        with pytest.raises(McAuthError):
            _check_nonce(token, nonce)
