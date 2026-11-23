# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Issue, check, and revoke scoped API tokens for machines.

Browser sessions and CSRF tokens suit people; a small process that sits
on another network segment and reports what it hears (the standalone
MNDP listener) needs a credential of its own. An administrator creates a
token in Settings with a name and the scopes it may use. LabRat shows
the token once and stores only a SHA-256 digest of its secret part: the
secret is 32 random bytes, so a fast digest is enough (there is nothing
to guess), and a database leak reveals no usable token.

A token reads ``labrat_<id>_<secret>``: the 12-character public id finds
the row, and the secret is compared in constant time. Tokens can expire,
are revoked rather than deleted (so the audit log keeps naming them),
and record when they were last used. A token is accepted only in an
``Authorization: Bearer`` header, on the machine endpoints that name one
of its scopes; it never opens the browser pages or the session API.

Used by: api.machine, pages.settings_tokens.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
import secrets

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from labrat.audit import record_audit_event
from labrat.database import Base, as_utc

SCOPES = {
    "observations:mndp": "Submit MikroTik announcements (MNDP) heard on another network segment",
}
"""Every scope a token may hold, with what it allows."""

TOKEN_PATTERN = re.compile(r"^labrat_([0-9a-f]{12})_([A-Za-z0-9_-]{43})$")
"""The shape of a token: public id, then the urlsafe-base64 secret."""

NAME_MAX = 80
"""Longest token name."""

EXPIRY_CHOICES = {"never": None, "30": 30, "90": 90, "365": 365}
"""Lifetimes an administrator may pick, in days (None for no expiry)."""

LAST_USED_GRANULARITY = timedelta(minutes=1)
"""How stale ``last_used_at`` may get before a request rewrites it."""


class ApiToken(Base):
    """One machine credential and what it may do.

    ``secret_digest`` is the hex SHA-256 of the secret part; the token
    itself is never stored. ``scopes`` is a JSON list of :data:`SCOPES`
    keys.
    """

    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(12), unique=True)
    name: Mapped[str] = mapped_column(String(NAME_MAX))
    secret_digest: Mapped[str] = mapped_column(String(64))
    scopes: Mapped[str] = mapped_column(Text, server_default="[]")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TokenError(ValueError):
    """An administrator's token request was invalid; the message says why."""


@dataclass(frozen=True)
class TokenView:
    """A token as Settings lists it: never the secret.

    Attributes:
        id: Row id.
        name: The administrator's name for it.
        prefix: ``labrat_<public id>_…``, enough to recognise it.
        scopes: Its scope keys.
        created_at: When it was made.
        expires_at: When it stops working, if ever.
        last_used_at: Its latest accepted request, if any.
        revoked_at: When it was revoked, if it was.
        status: ``active``, ``expired``, or ``revoked``.
    """

    id: int
    name: str
    prefix: str
    scopes: tuple[str, ...]
    created_at: datetime | None
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    status: str



def _digest(secret: str) -> str:
    """Digest a token's secret part.

    Args:
        secret: The secret after the public id.

    Returns:
        str: Hex SHA-256.
    """
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


def scopes_of(token: ApiToken) -> tuple[str, ...]:
    """Read a token's scopes, ignoring any that no longer exist.

    Args:
        token: The stored token.

    Returns:
        tuple[str, ...]: Known scope keys.
    """
    try:
        stored = json.loads(token.scopes or "[]")
    except ValueError:
        return ()
    return tuple(scope for scope in stored if isinstance(scope, str) and scope in SCOPES)


def status_of(token: ApiToken, now: datetime | None = None) -> str:
    """Say whether a token still works.

    Args:
        token: The stored token.
        now: The time to judge by; defaults to now.

    Returns:
        str: ``revoked``, ``expired``, or ``active``.
    """
    now = now or datetime.now(timezone.utc)
    if token.revoked_at is not None:
        return "revoked"
    expires = as_utc(token.expires_at)
    if expires is not None and expires <= now:
        return "expired"
    return "active"


def create_token(db: Session, *, name: str, scopes: list[str], expiry: str, creator_id: int, creator_name: str) -> tuple[ApiToken, str]:
    """Make a token, audit it, and return the only copy of its secret.

    Args:
        db: The caller's session; the caller commits.
        name: The administrator's name for it (1 to :data:`NAME_MAX` characters).
        scopes: Requested scope keys; at least one, all known.
        expiry: A key of :data:`EXPIRY_CHOICES`.
        creator_id: The administrator's user id.
        creator_name: The administrator's username, for the audit log.

    Returns:
        tuple[ApiToken, str]: The new row and the full token to show once.

    Raises:
        TokenError: The name, scopes, or expiry are invalid, or the name is taken.
    """
    name = (name or "").strip()
    if not 0 < len(name) <= NAME_MAX:
        raise TokenError(f"Give the token a name of 1 to {NAME_MAX} characters.")
    if db.scalar(select(ApiToken.id).where(ApiToken.name == name, ApiToken.revoked_at.is_(None))) is not None:
        raise TokenError("An active token already has that name.")
    chosen = sorted(set(scopes))
    if not chosen or any(scope not in SCOPES for scope in chosen):
        raise TokenError("Choose at least one of the listed permissions.")
    if expiry not in EXPIRY_CHOICES:
        raise TokenError("Choose how long the token lasts.")
    days = EXPIRY_CHOICES[expiry]
    public_id = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    token = ApiToken(
        public_id=public_id, name=name, secret_digest=_digest(secret), scopes=json.dumps(chosen),
        created_by=creator_id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=days) if days else None,
    )
    db.add(token)
    db.flush()
    record_audit_event(
        db, actor_kind="user", actor_id=creator_id, actor_name=creator_name,
        action="api_token.created", subject_kind="api_token", subject_id=token.id,
        details={"name": name, "scopes": chosen, "expires_days": days},
    )
    return token, f"labrat_{public_id}_{secret}"


def revoke_token(db: Session, token_id: int, *, actor_id: int, actor_name: str) -> ApiToken:
    """Revoke a token so it stops working at once.

    Args:
        db: The caller's session; the caller commits.
        token_id: The token's row id.
        actor_id: The administrator's user id.
        actor_name: The administrator's username.

    Returns:
        ApiToken: The revoked token.

    Raises:
        TokenError: No such token, or it is already revoked.
    """
    token = db.get(ApiToken, token_id)
    if token is None:
        raise TokenError("That token no longer exists.")
    if token.revoked_at is not None:
        raise TokenError("That token is already revoked.")
    token.revoked_at = datetime.now(timezone.utc)
    record_audit_event(
        db, actor_kind="user", actor_id=actor_id, actor_name=actor_name,
        action="api_token.revoked", subject_kind="api_token", subject_id=token.id,
        details={"name": token.name},
    )
    return token


def authenticate(db: Session, presented: str, scope: str) -> tuple[ApiToken | None, str]:
    """Check a presented token for one scope, and note that it was used.

    Args:
        db: The caller's session; the caller commits.
        presented: The bearer token from the request.
        scope: The scope the endpoint needs.

    Returns:
        tuple[ApiToken | None, str]: The token and ``ok``; or None and
            ``invalid`` (unknown, malformed, revoked, or expired: one answer,
            so a caller learns nothing about which) or ``forbidden`` (valid,
            without that scope).
    """
    match = TOKEN_PATTERN.match(presented or "")
    if match is None:
        return None, "invalid"
    token = db.scalar(select(ApiToken).where(ApiToken.public_id == match.group(1)))
    if token is None or not hmac.compare_digest(token.secret_digest, _digest(match.group(2))):
        return None, "invalid"
    now = datetime.now(timezone.utc)
    if status_of(token, now) != "active":
        return None, "invalid"
    if scope not in scopes_of(token):
        return None, "forbidden"
    last = as_utc(token.last_used_at)
    if last is None or now - last >= LAST_USED_GRANULARITY:
        token.last_used_at = now
    return token, "ok"


def list_tokens(db: Session) -> list[TokenView]:
    """List every token for Settings, newest first.

    Args:
        db: An open session.

    Returns:
        list[TokenView]: Tokens without their secrets.
    """
    now = datetime.now(timezone.utc)
    return [
        TokenView(
            id=token.id, name=token.name, prefix=f"labrat_{token.public_id}_…", scopes=scopes_of(token),
            created_at=as_utc(token.created_at), expires_at=as_utc(token.expires_at),
            last_used_at=as_utc(token.last_used_at), revoked_at=as_utc(token.revoked_at),
            status=status_of(token, now),
        )
        for token in db.scalars(select(ApiToken).order_by(ApiToken.id.desc()))
    ]
