"""Unit tests for app.core.security (no database)."""

import base64
import hashlib
import json
import statistics
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

from app.core import security
from app.core.config import AuthConfigurationError, Settings
from app.core.security import (
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    InvalidTokenError,
    PasswordPolicyError,
    check_password_policy,
    create_access_token,
    create_csrf_token,
    decode_access_token,
    generate_opaque_token,
    hash_opaque_token,
    hash_password,
    is_origin_allowed,
    normalize_origin,
    request_origin,
    verify_csrf_token,
    verify_dummy_password,
    verify_password,
)

JWT_SECRET = "jwt-test-secret-6f1d0a9b8c7e5d4c3b2a1f0e9d8c7b6a"
CSRF_SECRET = "csrf-test-secret-0a1b2c3d4e5f60718293a4b5c6d7e8f9"
PASSWORD = "correct horse battery staple"


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"jwt_secret": JWT_SECRET, "csrf_secret": CSRF_SECRET}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture
def settings() -> Settings:
    return make_settings()


# --- Password policy and hashing -------------------------------------------------------------


@pytest.mark.parametrize("length", [PASSWORD_MIN_LENGTH, 64, PASSWORD_MAX_LENGTH])
def test_password_policy_accepts_lengths_within_bounds(length: int) -> None:
    check_password_policy("p" * length)


@pytest.mark.parametrize("length", [0, 1, PASSWORD_MIN_LENGTH - 1, PASSWORD_MAX_LENGTH + 1, 10_000])
def test_password_policy_rejects_lengths_outside_bounds(length: int) -> None:
    password = "s3cr3t" * (length // 6 + 1)
    password = password[:length]
    with pytest.raises(PasswordPolicyError) as exc_info:
        check_password_policy(password)
    if len(password) >= 4:  # shorter strings may occur in the message by coincidence
        assert password not in str(exc_info.value)


def test_password_policy_has_no_composition_rules() -> None:
    for password in ("a" * 12, "123456789012", "            ", "ÄÖÜäöüßéèêëő"):
        check_password_policy(password)


def test_password_length_counts_characters_not_bytes() -> None:
    check_password_policy("é" * PASSWORD_MAX_LENGTH)  # 512 bytes in UTF-8, 256 characters
    with pytest.raises(PasswordPolicyError):
        check_password_policy("é" * (PASSWORD_MIN_LENGTH - 1))


def test_password_policy_rejects_non_strings() -> None:
    with pytest.raises(PasswordPolicyError):
        check_password_policy(None)  # type: ignore[arg-type]


def test_hash_is_argon2id_with_expected_parameters() -> None:
    password_hash = hash_password(PASSWORD)

    assert password_hash.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert PASSWORD not in password_hash


def test_hashes_are_salted() -> None:
    assert hash_password(PASSWORD) != hash_password(PASSWORD)


def test_hash_password_enforces_policy() -> None:
    with pytest.raises(PasswordPolicyError):
        hash_password("short")
    with pytest.raises(PasswordPolicyError):
        hash_password("x" * (PASSWORD_MAX_LENGTH + 1))


def test_verify_correct_and_wrong_password() -> None:
    password_hash = hash_password(PASSWORD)

    assert verify_password(PASSWORD, password_hash) == (True, None)
    assert verify_password(PASSWORD + " ", password_hash) == (False, None)
    assert verify_password(PASSWORD.upper(), password_hash) == (False, None)


def test_verify_unicode_password() -> None:
    password = "pässwörd-ünïcødé-🔐"
    password_hash = hash_password(password)

    assert verify_password(password, password_hash)[0] is True
    assert verify_password("passwoerd-unicode-🔐", password_hash)[0] is False


def test_outdated_hash_parameters_are_upgraded_on_verify() -> None:
    weak = PasswordHash((Argon2Hasher(time_cost=1, memory_cost=8192, parallelism=1),))
    old_hash = weak.hash(PASSWORD)

    valid, new_hash = verify_password(PASSWORD, old_hash)

    assert valid is True
    assert new_hash is not None and new_hash.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert verify_password(PASSWORD, new_hash) == (True, None)


@pytest.mark.parametrize(
    "stored_hash",
    [
        "",
        "not-a-hash",
        "$argon2id$v=19$m=65536,t=3,p=4$corrupted",
        "$2b$12$abcdefghijklmnopqrstuuVd7x/Vh2V3Ru0r/9C1Q6hE9xkY3H6tS",  # bcrypt: not accepted
        "5f4dcc3b5aa765d61d8327deb882cf99",  # unsalted MD5
    ],
)
def test_malformed_or_foreign_hashes_verify_as_invalid(stored_hash: str) -> None:
    assert verify_password(PASSWORD, stored_hash) == (False, None)


def test_overlong_or_non_string_password_verifies_as_invalid() -> None:
    password_hash = hash_password(PASSWORD)

    assert verify_password("x" * (PASSWORD_MAX_LENGTH + 1), password_hash) == (False, None)
    assert verify_password(None, password_hash) == (False, None)  # type: ignore[arg-type]


def _median_seconds(action: Any, runs: int = 5) -> float:
    durations = []
    for _ in range(runs):
        started = time.perf_counter()
        action()
        durations.append(time.perf_counter() - started)
    return statistics.median(durations)


def test_dummy_verification_costs_about_as_much_as_a_real_one() -> None:
    password_hash = hash_password(PASSWORD)
    verify_dummy_password("warm-up-the-cached-dummy-hash")

    real = _median_seconds(lambda: verify_password("wrong password!!", password_hash))
    dummy = _median_seconds(lambda: verify_dummy_password("wrong password!!"))
    malformed = _median_seconds(lambda: verify_password("wrong password!!", "not-a-hash"))

    # Same Argon2 work: unknown accounts and malformed hashes are not measurably faster.
    assert 0.33 < dummy / real < 3, (real, dummy)
    assert 0.33 < malformed / real < 3, (real, malformed)


def test_dummy_verification_tolerates_any_input() -> None:
    for candidate in ("", "x" * (PASSWORD_MAX_LENGTH + 10), None):
        verify_dummy_password(candidate)  # type: ignore[arg-type]


def test_dummy_hash_is_random_and_cached() -> None:
    first = security._dummy_password_hash()

    assert first == security._dummy_password_hash()
    assert first.startswith("$argon2id$")


# --- Access tokens ---------------------------------------------------------------------------


def _encode(claims: dict[str, Any], *, key: str = JWT_SECRET, algorithm: str = "HS256") -> str:
    return jwt.encode(claims, key, algorithm=algorithm)


def _valid_claims(**overrides: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "iss": "traceiq",
        "aud": "traceiq-api",
        "sub": str(uuid.uuid4()),
        "sid": str(uuid.uuid4()),
        "jti": "jti-1",
        "typ": "access",
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=15),
    }
    claims.update(overrides)
    return claims


def test_access_token_round_trip(settings: Settings) -> None:
    user_id, session_id = uuid.uuid4(), uuid.uuid4()

    token, expires_at = create_access_token(user_id=user_id, session_id=session_id, settings=settings)
    claims = decode_access_token(token, settings=settings)

    assert (claims.user_id, claims.session_id) == (user_id, session_id)
    assert claims.expires_at == expires_at
    assert claims.expires_at - claims.issued_at == timedelta(seconds=15 * 60)
    header = jwt.get_unverified_header(token)
    assert (header["alg"], header["typ"]) == ("HS256", "JWT")
    payload = jwt.decode(token, options={"verify_signature": False})
    assert (payload["iss"], payload["aud"], payload["typ"]) == ("traceiq", "traceiq-api", "access")


def test_access_token_lifetime_follows_settings() -> None:
    settings = make_settings(access_token_ttl_seconds=60)
    token, _ = create_access_token(user_id=uuid.uuid4(), session_id=uuid.uuid4(), settings=settings)

    claims = decode_access_token(token, settings=settings)
    assert claims.expires_at - claims.issued_at == timedelta(seconds=60)


def test_each_access_token_has_a_unique_id(settings: Settings) -> None:
    user_id, session_id = uuid.uuid4(), uuid.uuid4()
    tokens = [create_access_token(user_id=user_id, session_id=session_id, settings=settings)[0] for _ in range(20)]

    assert len({decode_access_token(token, settings=settings).token_id for token in tokens}) == 20


def test_expired_access_token_is_rejected(settings: Settings) -> None:
    past = datetime.now(UTC) - timedelta(minutes=16)
    token, _ = create_access_token(user_id=uuid.uuid4(), session_id=uuid.uuid4(), settings=settings, now=past)

    with pytest.raises(InvalidTokenError):
        decode_access_token(token, settings=settings)


def test_leeway_allows_small_clock_skew_only(settings: Settings) -> None:
    now = datetime.now(UTC)
    issued = now - timedelta(minutes=16)
    just_expired = _encode(_valid_claims(iat=issued, nbf=issued, exp=now - timedelta(seconds=5)))
    long_expired = _encode(_valid_claims(iat=issued, nbf=issued, exp=now - timedelta(seconds=30)))

    decode_access_token(just_expired, settings=settings)  # within the 10-second leeway
    with pytest.raises(InvalidTokenError):
        decode_access_token(long_expired, settings=settings)


def test_not_yet_valid_access_token_is_rejected(settings: Settings) -> None:
    future = datetime.now(UTC) + timedelta(minutes=5)
    token = _encode(_valid_claims(iat=future, nbf=future, exp=future + timedelta(minutes=15)))

    with pytest.raises(InvalidTokenError):
        decode_access_token(token, settings=settings)


def test_tampered_payload_is_rejected(settings: Settings) -> None:
    token, _ = create_access_token(user_id=uuid.uuid4(), session_id=uuid.uuid4(), settings=settings)
    header, payload, signature = token.split(".")
    claims = jwt.decode(token, options={"verify_signature": False})
    claims["sub"] = str(uuid.uuid4())
    forged_payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()

    with pytest.raises(InvalidTokenError):
        decode_access_token(f"{header}.{forged_payload}.{signature}", settings=settings)
    with pytest.raises(InvalidTokenError):
        decode_access_token(f"{header}.{payload}.{signature[:-2]}AA", settings=settings)


def test_token_signed_with_another_secret_is_rejected(settings: Settings) -> None:
    token = _encode(_valid_claims(), key="another-secret-of-sufficient-length-1234567890")

    with pytest.raises(InvalidTokenError):
        decode_access_token(token, settings=settings)


def test_unsigned_alg_none_token_is_rejected(settings: Settings) -> None:
    token = jwt.encode(_valid_claims(), key=None, algorithm="none")  # type: ignore[arg-type]

    with pytest.raises(InvalidTokenError):
        decode_access_token(token, settings=settings)


@pytest.mark.filterwarnings("ignore::jwt.warnings.InsecureKeyLengthWarning")
@pytest.mark.parametrize("algorithm", ["HS384", "HS512"])
def test_other_algorithms_are_rejected_even_with_the_right_secret(settings: Settings, algorithm: str) -> None:
    token = _encode(_valid_claims(), algorithm=algorithm)

    with pytest.raises(InvalidTokenError):
        decode_access_token(token, settings=settings)


@pytest.mark.parametrize("claim", ["exp", "iat", "nbf", "iss", "aud", "sub", "sid", "jti", "typ"])
def test_missing_required_claim_is_rejected(settings: Settings, claim: str) -> None:
    claims = _valid_claims()
    del claims[claim]

    with pytest.raises(InvalidTokenError):
        decode_access_token(_encode(claims), settings=settings)


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "someone-else"},
        {"aud": "another-service"},
        {"aud": ["another-service"]},
        {"typ": "refresh"},
        {"typ": "ACCESS"},
        {"sub": "not-a-uuid"},
        {"sid": "not-a-uuid"},
        {"sub": 42},
    ],
)
def test_wrong_claim_values_are_rejected(settings: Settings, overrides: dict[str, Any]) -> None:
    with pytest.raises(InvalidTokenError):
        decode_access_token(_encode(_valid_claims(**overrides)), settings=settings)


def test_issuer_and_audience_follow_settings() -> None:
    settings = make_settings(jwt_issuer="issuer-a", jwt_audience="audience-a")
    token, _ = create_access_token(user_id=uuid.uuid4(), session_id=uuid.uuid4(), settings=settings)

    decode_access_token(token, settings=settings)
    with pytest.raises(InvalidTokenError):
        decode_access_token(token, settings=make_settings(jwt_issuer="issuer-b", jwt_audience="audience-a"))
    with pytest.raises(InvalidTokenError):
        decode_access_token(token, settings=make_settings(jwt_issuer="issuer-a", jwt_audience="audience-b"))


@pytest.mark.parametrize("token", ["", "garbage", "a.b.c", "Bearer x.y.z", "x" * 5000, None, 123])
def test_malformed_tokens_are_rejected_with_a_generic_message(settings: Settings, token: Any) -> None:
    with pytest.raises(InvalidTokenError) as exc_info:
        decode_access_token(token, settings=settings)
    assert str(exc_info.value) == "Invalid access token"


def test_error_message_never_contains_the_token(settings: Settings) -> None:
    past = datetime.now(UTC) - timedelta(hours=1)
    token, _ = create_access_token(user_id=uuid.uuid4(), session_id=uuid.uuid4(), settings=settings, now=past)

    with pytest.raises(InvalidTokenError) as exc_info:
        decode_access_token(token, settings=settings)
    assert token not in str(exc_info.value)
    assert exc_info.value.__cause__ is None  # PyJWT details are not chained


def test_missing_jwt_secret_is_a_configuration_error() -> None:
    settings = make_settings(jwt_secret=None)

    with pytest.raises(AuthConfigurationError):
        create_access_token(user_id=uuid.uuid4(), session_id=uuid.uuid4(), settings=settings)
    with pytest.raises(AuthConfigurationError):
        decode_access_token(_encode(_valid_claims()), settings=settings)


# --- Opaque tokens ---------------------------------------------------------------------------


def test_opaque_tokens_carry_32_random_bytes() -> None:
    token = generate_opaque_token()
    raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))

    assert len(raw) == 32
    assert set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def test_opaque_tokens_are_unique() -> None:
    assert len({generate_opaque_token() for _ in range(1000)}) == 1000


def test_opaque_token_hash_is_sha256_hex() -> None:
    token = generate_opaque_token()

    digest = hash_opaque_token(token)

    assert digest == hashlib.sha256(token.encode()).hexdigest()
    assert len(digest) == 64 and token not in digest
    assert hash_opaque_token(token) == digest  # deterministic, so it can be looked up
    assert hash_opaque_token(generate_opaque_token()) != digest


@pytest.mark.parametrize("token", ["", None, 123, "x" * 513])
def test_invalid_opaque_tokens_are_rejected(token: Any) -> None:
    with pytest.raises(InvalidTokenError):
        hash_opaque_token(token)


# --- CSRF tokens -----------------------------------------------------------------------------


def test_csrf_token_round_trip(settings: Settings) -> None:
    session_id = uuid.uuid4()
    token = create_csrf_token(session_id, settings=settings)

    nonce, signature = token.split(".")
    assert len(nonce) == 43 and len(signature) == 43  # 32-byte nonce, 32-byte HMAC-SHA256
    assert verify_csrf_token(header_token=token, cookie_token=token, session_id=session_id, settings=settings)


def test_csrf_tokens_are_unique(settings: Settings) -> None:
    session_id = uuid.uuid4()
    assert create_csrf_token(session_id, settings=settings) != create_csrf_token(session_id, settings=settings)


def test_csrf_header_must_match_cookie(settings: Settings) -> None:
    session_id = uuid.uuid4()
    first = create_csrf_token(session_id, settings=settings)
    second = create_csrf_token(session_id, settings=settings)

    # Both are validly signed for this session, but double-submit requires the same value.
    assert not verify_csrf_token(header_token=first, cookie_token=second, session_id=session_id, settings=settings)


@pytest.mark.parametrize(("header", "cookie"), [(None, "x"), ("x", None), ("", ""), (None, None)])
def test_csrf_missing_header_or_cookie_is_rejected(settings: Settings, header: Any, cookie: Any) -> None:
    session_id = uuid.uuid4()
    token = create_csrf_token(session_id, settings=settings)
    header = token if header == "x" else header
    cookie = token if cookie == "x" else cookie

    assert not verify_csrf_token(header_token=header, cookie_token=cookie, session_id=session_id, settings=settings)


def test_csrf_token_is_bound_to_its_session(settings: Settings) -> None:
    token = create_csrf_token(uuid.uuid4(), settings=settings)

    assert not verify_csrf_token(header_token=token, cookie_token=token, session_id=uuid.uuid4(), settings=settings)


def test_csrf_token_signed_with_another_secret_is_rejected(settings: Settings) -> None:
    session_id = uuid.uuid4()
    other = make_settings(csrf_secret="a-different-csrf-secret-0123456789abcdefghij")
    token = create_csrf_token(session_id, settings=other)

    assert not verify_csrf_token(header_token=token, cookie_token=token, session_id=session_id, settings=settings)


def test_csrf_token_is_not_signed_with_the_jwt_secret(settings: Settings) -> None:
    session_id = uuid.uuid4()
    swapped = make_settings(jwt_secret=CSRF_SECRET, csrf_secret=JWT_SECRET)
    token = create_csrf_token(session_id, settings=swapped)

    assert not verify_csrf_token(header_token=token, cookie_token=token, session_id=session_id, settings=settings)


def test_tampered_or_malformed_csrf_tokens_are_rejected(settings: Settings) -> None:
    session_id = uuid.uuid4()
    nonce, signature = create_csrf_token(session_id, settings=settings).split(".")
    candidates = [
        f"{nonce}.{signature[:-1]}{'A' if signature[-1] != 'A' else 'B'}",
        f"{nonce[:-1]}{'A' if nonce[-1] != 'A' else 'B'}.{signature}",
        f"{nonce}{signature}",
        f".{signature}",
        f"{nonce}.",
        ".",
        f"{nonce}.{signature}.extra",
        "ñonce.signature",
        "x" * 300,
    ]
    for candidate in candidates:
        assert not verify_csrf_token(
            header_token=candidate, cookie_token=candidate, session_id=session_id, settings=settings
        ), candidate


def test_missing_csrf_secret_is_a_configuration_error() -> None:
    settings = make_settings(csrf_secret=None)

    with pytest.raises(AuthConfigurationError):
        create_csrf_token(uuid.uuid4(), settings=settings)


# --- Origin validation -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("http://localhost:5173", "http://localhost:5173"),
        ("HTTP://LocalHost:5173", "http://localhost:5173"),
        ("https://app.example.com", "https://app.example.com"),
        ("https://app.example.com:443", "https://app.example.com"),
        ("http://app.example.com:80", "http://app.example.com"),
        ("https://app.example.com:8443", "https://app.example.com:8443"),
        ("http://localhost:5173/", "http://localhost:5173"),
        ("http://localhost:5173/login?next=/x#frag", "http://localhost:5173"),  # Referer
        ("  http://localhost:5173  ", "http://localhost:5173"),
        ("http://[::1]:5173", "http://[::1]:5173"),
        ("null", None),
        ("NULL", None),
        ("", None),
        (None, None),
        ("localhost:5173", None),
        ("file:///etc/passwd", None),
        ("javascript:alert(1)", None),
        ("data:text/html,x", None),
        ("ftp://example.com", None),
        ("http://user:pw@example.com", None),
        ("http://example.com:99999", None),
        ("http://", None),
        ("http://" + "a" * 3000 + ".com", None),
    ],
)
def test_normalize_origin(value: Any, expected: str | None) -> None:
    assert normalize_origin(value) == expected


def test_request_origin_prefers_origin_header() -> None:
    assert request_origin("http://localhost:5173", "http://evil.test/page") == "http://localhost:5173"


def test_request_origin_falls_back_to_referer_only_when_origin_is_absent() -> None:
    assert request_origin(None, "http://localhost:5173/login") == "http://localhost:5173"
    assert request_origin("", "http://localhost:5173/login") == "http://localhost:5173"
    assert request_origin(None, None) is None


@pytest.mark.parametrize("origin", ["null", "not a url", "file:///x"])
def test_invalid_origin_header_does_not_fall_back_to_referer(origin: str) -> None:
    assert request_origin(origin, "http://localhost:5173/login") is None


ALLOWED = ["http://localhost:5173", "https://app.example.com"]


@pytest.mark.parametrize(
    "origin",
    ["http://localhost:5173", "HTTP://LOCALHOST:5173", "https://app.example.com", "https://app.example.com:443"],
)
def test_allowed_origins(origin: str) -> None:
    assert is_origin_allowed(origin, ALLOWED)


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:5174",  # different port
        "https://localhost:5173",  # different scheme
        "http://127.0.0.1:5173",  # different host spelling
        "http://app.example.com",  # http instead of https
        "https://evil.app.example.com",  # subdomain
        "https://app.example.com.evil.test",  # suffix trick
        "https://appexample.com",
        "http://localhost",
        "null",
        "",
        None,
    ],
)
def test_disallowed_origins(origin: Any) -> None:
    assert not is_origin_allowed(origin, ALLOWED)


def test_wildcard_allowlist_entry_never_allows() -> None:
    assert not is_origin_allowed("http://localhost:5173", ["*"])
    assert not is_origin_allowed("https://anything.test", ["*", "null"])


def test_allowlist_entries_are_normalized() -> None:
    assert is_origin_allowed("http://localhost:5173", ["HTTP://LOCALHOST:5173/"])
    assert is_origin_allowed("https://app.example.com", ["https://app.example.com:443"])


def test_empty_allowlist_allows_nothing() -> None:
    assert not is_origin_allowed("http://localhost:5173", [])


# --- Concurrency bound on Argon2id work -------------------------------------------------------


class _CountingHasher:
    """Stands in for pwdlib's PasswordHash; records how many calls run at the same time."""

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self.calls = 0
        self._lock = threading.Lock()

    def _work(self) -> None:
        with self._lock:
            self.active += 1
            self.calls += 1
            self.peak = max(self.peak, self.active)
        time.sleep(0.05)
        with self._lock:
            self.active -= 1

    def hash(self, password: str) -> str:
        self._work()
        return "$fake$hash"

    def verify(self, password: str, password_hash: str) -> bool:
        self._work()
        return False

    def verify_and_update(self, password: str, password_hash: str) -> tuple[bool, str | None]:
        self._work()
        return False, None


def _limit_hashing(monkeypatch: pytest.MonkeyPatch, limit: int) -> None:
    settings = security.get_settings().model_copy(update={"auth_max_concurrent_hashes": limit})
    monkeypatch.setattr(security, "get_settings", lambda: settings)
    security._hashing_semaphore.cache_clear()


@pytest.fixture
def counting_hasher(monkeypatch: pytest.MonkeyPatch) -> Iterator[_CountingHasher]:
    """Two hashing slots, and a counting fake in place of the real hasher."""
    fake = _CountingHasher()
    _limit_hashing(monkeypatch, 2)
    monkeypatch.setattr(security, "_password_hash", fake)
    monkeypatch.setattr(security, "_dummy_password_hash", lambda: "$fake$dummy")
    yield fake
    security._hashing_semaphore.cache_clear()


def _run_concurrently(actions: list[Callable[[], object]]) -> None:
    errors: list[BaseException] = []

    def run(action: Callable[[], object]) -> None:
        try:
            action()
        except BaseException as exc:  # pragma: no cover - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(action,)) for action in actions]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors
    assert not any(thread.is_alive() for thread in threads)


def test_concurrent_hashing_is_bounded(counting_hasher: _CountingHasher) -> None:
    _run_concurrently([lambda: security.hash_password(PASSWORD)] * 10)

    assert counting_hasher.calls == 10
    assert counting_hasher.peak == 2


def test_all_hashing_paths_share_one_bound(counting_hasher: _CountingHasher) -> None:
    actions: list[Callable[[], object]] = [
        lambda: security.hash_password(PASSWORD),
        lambda: security.verify_password(PASSWORD, "$argon2id$stored"),
        lambda: security.verify_password("x" * 300, "$argon2id$stored"),  # over-long: dummy path
        lambda: security.verify_dummy_password(PASSWORD),
    ] * 4

    _run_concurrently(actions)

    assert counting_hasher.calls == 16
    assert counting_hasher.peak == 2


def test_dummy_fallback_does_not_take_a_second_slot(
    counting_hasher: _CountingHasher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed stored hash falls back to a dummy verification while the slot is held. Taking a
    second slot would deadlock with a single slot."""
    _limit_hashing(monkeypatch, 1)

    def malformed(password: str, password_hash: str) -> tuple[bool, str | None]:
        raise ValueError("malformed hash")

    monkeypatch.setattr(counting_hasher, "verify_and_update", malformed)

    _run_concurrently([lambda: security.verify_password(PASSWORD, "not-a-hash")] * 3)

    assert counting_hasher.calls == 3  # one dummy verification each
    assert counting_hasher.peak == 1


def test_the_semaphore_is_shared_across_calls(counting_hasher: _CountingHasher) -> None:
    first = security._hashing_semaphore()

    security.hash_password(PASSWORD)
    security.verify_dummy_password(PASSWORD)
    security.verify_password(PASSWORD, "$argon2id$stored")

    assert security._hashing_semaphore() is first


def test_slots_are_released_after_errors(counting_hasher: _CountingHasher, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_hash(password: str) -> str:
        raise RuntimeError("hasher failed")

    monkeypatch.setattr(counting_hasher, "hash", failing_hash)
    for _ in range(5):  # more failures than slots: each one must release its slot
        with pytest.raises(RuntimeError):
            security.hash_password(PASSWORD)

    semaphore = security._hashing_semaphore()
    assert [semaphore.acquire(blocking=False) for _ in range(3)] == [True, True, False]
    semaphore.release()
    semaphore.release()


def test_default_semaphore_uses_the_configured_limit() -> None:
    security._hashing_semaphore.cache_clear()
    try:
        semaphore = security._hashing_semaphore()
        limit = security.get_settings().auth_max_concurrent_hashes
        acquired = [semaphore.acquire(blocking=False) for _ in range(limit + 1)]
        assert acquired == [True] * limit + [False]
        for _ in range(limit):
            semaphore.release()
    finally:
        security._hashing_semaphore.cache_clear()


def test_real_hashing_still_works_under_the_bound() -> None:
    hashed = security.hash_password(PASSWORD)

    assert security.verify_password(PASSWORD, hashed) == (True, None)
    assert security.verify_password("wrong password!!", hashed) == (False, None)
