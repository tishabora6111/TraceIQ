"""Security primitives: password hashing, access tokens, opaque tokens, CSRF tokens, Origin checks.

Domain-free; the auth module builds on these. Nothing here logs or returns secret values.

* Passwords: Argon2id via pwdlib, with a dummy verification for unknown accounts so that response
  time does not reveal whether an account exists.
* Access tokens: HS256 JWTs via PyJWT. Decoding accepts only HS256 and requires every claim this
  module issues.
* Opaque tokens (refresh tokens): 32 random bytes; only their SHA-256 digest is stored.
* CSRF tokens: ``<nonce>.<HMAC-SHA256(csrf_secret, session id + nonce)>``, bound to a session
  (refresh-token family); compared in constant time.
* Origin validation: exact scheme/host/port match against the configured allowlist.
"""

import base64
import hashlib
import hmac
import secrets
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

import jwt
from pwdlib import PasswordHash
from pwdlib.exceptions import PwdlibError
from pwdlib.hashers.argon2 import Argon2Hasher

from app.core.config import AuthConfigurationError, Settings, get_settings

# --- Passwords -------------------------------------------------------------------------------

PASSWORD_MIN_LENGTH = 12
# Upper bound keeps hashing cost bounded for hostile input.
PASSWORD_MAX_LENGTH = 256

# pwdlib's Argon2id defaults (argon2-cffi): 64 MiB memory, 3 iterations, parallelism 4.
_password_hash = PasswordHash((Argon2Hasher(),))


class PasswordPolicyError(ValueError):
    """The password does not meet the length policy. The message never contains the password."""


def check_password_policy(password: str) -> None:
    if not isinstance(password, str):
        raise PasswordPolicyError("Password must be a string")
    if len(password) < PASSWORD_MIN_LENGTH:
        raise PasswordPolicyError(f"Password must be at least {PASSWORD_MIN_LENGTH} characters")
    if len(password) > PASSWORD_MAX_LENGTH:
        raise PasswordPolicyError(f"Password must be at most {PASSWORD_MAX_LENGTH} characters")


@lru_cache(maxsize=1)
def _hashing_semaphore() -> threading.BoundedSemaphore:
    """One process-wide semaphore bounding concurrent Argon2id work (and so its memory use)."""
    return threading.BoundedSemaphore(get_settings().auth_max_concurrent_hashes)


class PasswordHashingBusyError(Exception):
    """No hashing slot became free within ``auth_hash_wait_timeout_seconds`` (the server is overloaded)."""


@contextmanager
def _hashing_slot() -> Iterator[None]:
    """Hold one hashing slot. Every hash and verification, real or dummy, runs inside exactly one.

    Waits at most ``auth_hash_wait_timeout_seconds``, then raises ``PasswordHashingBusyError``.
    Never nest: the functions below call only the unguarded helpers while holding a slot.
    """
    semaphore = _hashing_semaphore()
    if not semaphore.acquire(timeout=get_settings().auth_hash_wait_timeout_seconds):
        raise PasswordHashingBusyError("No password hashing capacity available")
    try:
        yield
    finally:
        semaphore.release()


def hash_password(password: str) -> str:
    """Hash a password that satisfies the policy."""
    check_password_policy(password)
    with _hashing_slot():
        return _password_hash.hash(password)


def verify_password(password: str, password_hash: str) -> tuple[bool, str | None]:
    """Verify ``password`` against ``password_hash``.

    Returns ``(valid, new_hash)``; ``new_hash`` is set when the stored hash uses outdated
    parameters and should be replaced. Malformed hashes and over-long passwords verify as
    invalid (an over-long password still costs one dummy verification, to keep timing uniform).
    """
    with _hashing_slot():
        if not isinstance(password, str) or len(password) > PASSWORD_MAX_LENGTH:
            _verify_dummy("")
            return False, None
        try:
            return _password_hash.verify_and_update(password, password_hash)
        except (PwdlibError, ValueError, TypeError):
            _verify_dummy(password)
            return False, None


@lru_cache(maxsize=1)
def _dummy_password_hash() -> str:
    return _password_hash.hash(secrets.token_urlsafe(32))


def verify_dummy_password(password: str) -> None:
    """Spend the same work as a real verification and discard the result.

    Used when no account matches, so response time does not reveal whether an email exists.
    Takes a hashing slot like a real verification, so it queues identically under load.
    """
    with _hashing_slot():
        _verify_dummy(password)


def _verify_dummy(password: str) -> None:
    """Dummy verification without taking a slot; the caller must hold one."""
    candidate = password if isinstance(password, str) and len(password) <= PASSWORD_MAX_LENGTH else ""
    try:
        _password_hash.verify(candidate, _dummy_password_hash())
    except (PwdlibError, ValueError, TypeError):  # pragma: no cover - the dummy hash is always valid
        pass


# --- Access tokens (JWT) ---------------------------------------------------------------------

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_TYPE = "access"
_REQUIRED_CLAIMS = ["exp", "iat", "nbf", "iss", "aud", "sub", "sid", "jti", "typ"]


class InvalidTokenError(Exception):
    """A token is missing, malformed, expired, or fails verification.

    The message is generic on purpose; it never contains the token.
    """


@dataclass(frozen=True)
class AccessTokenClaims:
    user_id: uuid.UUID
    # The refresh-token family (session) the access token was issued for.
    session_id: uuid.UUID
    token_id: str
    issued_at: datetime
    expires_at: datetime


def _jwt_key(settings: Settings) -> bytes:
    if settings.jwt_secret is None:
        raise AuthConfigurationError("TRACEIQ_JWT_SECRET is not set")
    return settings.jwt_secret.get_secret_value().encode("utf-8")


def create_access_token(
    *, user_id: uuid.UUID, session_id: uuid.UUID, settings: Settings, now: datetime | None = None
) -> tuple[str, datetime]:
    """Return ``(token, expires_at)`` for a short-lived access token."""
    issued_at = (now or datetime.now(UTC)).replace(microsecond=0)
    expires_at = issued_at + timedelta(seconds=settings.access_token_ttl_seconds)
    claims: dict[str, Any] = {
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "sub": str(user_id),
        "sid": str(session_id),
        "jti": secrets.token_urlsafe(16),
        "typ": ACCESS_TOKEN_TYPE,
        "iat": issued_at,
        "nbf": issued_at,
        "exp": expires_at,
    }
    token = jwt.encode(claims, _jwt_key(settings), algorithm=JWT_ALGORITHM, headers={"typ": "JWT"})
    return token, expires_at


def decode_access_token(token: str, *, settings: Settings) -> AccessTokenClaims:
    """Verify an access token and return its claims, or raise ``InvalidTokenError``.

    Only HS256 is accepted (``alg: none`` and other algorithms are rejected), the signature,
    issuer, audience, and time claims are verified, and the token type must be ``access``.
    """
    if not isinstance(token, str) or not token or len(token) > 4096:
        raise InvalidTokenError("Invalid access token")
    try:
        payload = jwt.decode(
            token,
            _jwt_key(settings),
            algorithms=[JWT_ALGORITHM],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            leeway=settings.jwt_leeway_seconds,
            options={"require": _REQUIRED_CLAIMS, "verify_signature": True},
        )
        if payload.get("typ") != ACCESS_TOKEN_TYPE:
            raise InvalidTokenError("Invalid access token")
        # uuid.UUID() raises AttributeError (not ValueError) for non-strings; check types first.
        if not isinstance(payload["sub"], str) or not isinstance(payload["sid"], str):
            raise InvalidTokenError("Invalid access token")
        return AccessTokenClaims(
            user_id=uuid.UUID(payload["sub"]),
            session_id=uuid.UUID(payload["sid"]),
            token_id=str(payload["jti"]),
            issued_at=datetime.fromtimestamp(payload["iat"], UTC),
            expires_at=datetime.fromtimestamp(payload["exp"], UTC),
        )
    except InvalidTokenError:
        raise
    except (jwt.PyJWTError, ValueError, TypeError, KeyError, OverflowError):
        raise InvalidTokenError("Invalid access token") from None


# --- Opaque tokens (refresh tokens) ----------------------------------------------------------

OPAQUE_TOKEN_BYTES = 32
_MAX_OPAQUE_TOKEN_LENGTH = 512


def generate_opaque_token() -> str:
    """A URL-safe token carrying ``OPAQUE_TOKEN_BYTES`` random bytes."""
    return secrets.token_urlsafe(OPAQUE_TOKEN_BYTES)


def hash_opaque_token(token: str) -> str:
    """SHA-256 hex digest used to store and look up opaque tokens.

    A fast hash is appropriate: the token is 32 random bytes, so it cannot be brute-forced.
    """
    if not isinstance(token, str) or not token or len(token) > _MAX_OPAQUE_TOKEN_LENGTH:
        raise InvalidTokenError("Invalid token")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --- CSRF tokens -----------------------------------------------------------------------------

_CSRF_CONTEXT = b"traceiq.csrf.v1"
_CSRF_NONCE_BYTES = 32
_MAX_CSRF_TOKEN_LENGTH = 256


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _csrf_signature(session_id: uuid.UUID, nonce: str, settings: Settings) -> str:
    if settings.csrf_secret is None:
        raise AuthConfigurationError("TRACEIQ_CSRF_SECRET is not set")
    message = b"|".join((_CSRF_CONTEXT, str(session_id).encode("ascii"), nonce.encode("ascii")))
    digest = hmac.new(settings.csrf_secret.get_secret_value().encode("utf-8"), message, hashlib.sha256).digest()
    return _b64(digest)


def create_csrf_token(session_id: uuid.UUID, *, settings: Settings) -> str:
    """A new CSRF token bound to ``session_id`` (the refresh-token family)."""
    nonce = _b64(secrets.token_bytes(_CSRF_NONCE_BYTES))
    return f"{nonce}.{_csrf_signature(session_id, nonce, settings)}"


def verify_csrf_token(
    *, header_token: str | None, cookie_token: str | None, session_id: uuid.UUID, settings: Settings
) -> bool:
    """Signed double-submit check: header and cookie match, and the signature binds them to the session."""
    if not header_token or not cookie_token:
        return False
    if len(header_token) > _MAX_CSRF_TOKEN_LENGTH or len(cookie_token) > _MAX_CSRF_TOKEN_LENGTH:
        return False
    if not hmac.compare_digest(header_token.encode("utf-8"), cookie_token.encode("utf-8")):
        return False
    nonce, separator, signature = header_token.partition(".")
    if not separator or not nonce or not signature or not nonce.isascii():
        return False
    expected = _csrf_signature(session_id, nonce, settings)
    return hmac.compare_digest(signature.encode("utf-8"), expected.encode("ascii"))


# --- Origin validation -----------------------------------------------------------------------

_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_origin(value: str | None) -> str | None:
    """``scheme://host[:port]`` in lowercase with default ports removed, or ``None`` if invalid.

    Accepts an Origin header value or a full URL (e.g. a Referer). ``null`` and anything other
    than http(s) with a host are invalid.
    """
    if not value or not isinstance(value, str) or len(value) > 2048:
        return None
    value = value.strip()
    if value.lower() == "null":
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in _DEFAULT_PORTS or not host or parts.username is not None or parts.password is not None:
        return None
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    if port is None or port == _DEFAULT_PORTS[scheme]:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def request_origin(origin_header: str | None, referer_header: str | None) -> str | None:
    """The origin a browser request came from: the Origin header, or the Referer's origin if Origin is absent.

    If an Origin header is present but invalid (including ``null``), Referer is not consulted.
    """
    if origin_header is not None and origin_header.strip():
        return normalize_origin(origin_header)
    return normalize_origin(referer_header)


def is_origin_allowed(origin: str | None, allowed_origins: list[str]) -> bool:
    """Exact match of a normalized origin against the allowlist. A wildcard is never accepted."""
    normalized = normalize_origin(origin)
    if normalized is None:
        return False
    allowed = {normalized_allowed for entry in allowed_origins if (normalized_allowed := normalize_origin(entry))}
    return normalized in allowed
