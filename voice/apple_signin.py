"""Checks a Sign in with Apple identity token: signed by Apple, made for this app, still fresh, and carrying the
one-time value (nonce) this server handed out, so a token copied off the wire cannot be used again.

The app gets the token from Apple's own sign-in sheet and sends it once (POST /auth/apple); the server answers
with its own sign-in token (voice/people.py). Apple's public keys come from APPLE_KEYS_URL and are cached by
PyJWT's key client.
"""

import hashlib
import hmac
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

import jwt

APPLE_ISSUER = "https://appleid.apple.com"
APPLE_KEYS_URL = "https://appleid.apple.com/auth/keys"
NONCE_SECONDS = 600  # a nonce must come back within this long (arbitrary; Apple's tokens last 10 minutes too)
MAX_NONCES = 1000  # outstanding at once; past this the oldest are dropped (arbitrary, far above a few people's use)

_client: jwt.PyJWKClient | None = None


class SignInError(ValueError):
    pass


@dataclass(frozen=True)
class AppleIdentity:
    sub: str  # Apple's stable id for this person and this developer team
    email: str | None  # may be Apple's private relay address, or missing


def apple_key(token: str):
    """Apple's public key that signed `token` (blocking: it may fetch Apple's key list)."""
    global _client
    if _client is None:
        _client = jwt.PyJWKClient(APPLE_KEYS_URL, cache_keys=True, lifespan=3600)
    return _client.get_signing_key_from_jwt(token).key


def verify(identity_token: str, audience: str, nonce: str, key_for: Callable[[str], object] = apple_key) -> AppleIdentity:
    """The identity in an Apple identity token, or SignInError. `audience` is the app's bundle id, `nonce` the
    raw value the app hashed into its sign-in request."""
    try:
        claims = jwt.decode(identity_token, key_for(identity_token), algorithms=["RS256"], audience=audience,
                            issuer=APPLE_ISSUER, options={"require": ["exp", "iat", "sub", "nonce"]})
    except (jwt.PyJWTError, KeyError, ValueError) as exc:
        raise SignInError(f"Apple's sign-in token was not accepted: {exc}") from None
    expected = hashlib.sha256(nonce.encode()).hexdigest()
    if not hmac.compare_digest(str(claims["nonce"]), expected):
        raise SignInError("the sign-in token was made for another request")
    email = claims.get("email")
    return AppleIdentity(sub=str(claims["sub"]), email=email if isinstance(email, str) else None)


class Nonces:
    """One-time values for sign-in, kept in memory (one token server process)."""

    def __init__(self) -> None:
        self._issued: OrderedDict[str, float] = OrderedDict()  # oldest first

    def new(self) -> str:
        now = time.monotonic()
        while self._issued and (len(self._issued) >= MAX_NONCES
                                or now - next(iter(self._issued.values())) >= NONCE_SECONDS):
            self._issued.popitem(last=False)
        nonce = secrets.token_urlsafe(24)
        self._issued[nonce] = now
        return nonce

    def use(self, nonce: str) -> bool:
        """True once for a nonce this process issued recently; never again."""
        issued = self._issued.pop(nonce, None)
        return issued is not None and time.monotonic() - issued < NONCE_SECONDS
