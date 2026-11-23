# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Authenticate LabRat identities without coupling routes to credential types.

The local provider verifies Argon2id password hashes stored in SQLite. Future
SSO integrations can implement ``AuthenticationProvider`` and return the same
stable local user identity, leaving sessions and authorization unchanged.

Used by: app, pages.private, pages.public.
"""

from dataclasses import dataclass
import secrets
from typing import Protocol

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from labrat.models import LocalCredential, User


#: Shortest and longest local password accepted when one is set.
PASSWORD_LENGTH = (12, 1024)


def new_password_problem(password: str, confirm: str) -> str | None:
    """Check a new local password and its repeat, without logging either.

    Args:
        password: Proposed plaintext password.
        confirm: The same password typed again.

    Returns:
        str | None: Human-readable problem, or ``None`` when acceptable.
    """
    shortest, longest = PASSWORD_LENGTH
    if not shortest <= len(password) <= longest:
        return f"Choose a password between {shortest} and {longest} characters."
    if password != confirm:
        return "Passwords do not match."
    return None


@dataclass(frozen=True)
class AuthenticationResult:
    """Identify an authenticated local user and the provider used.

    Attributes:
        user_id: Stable primary key of a LabRat user.
        provider: Provider identifier, such as ``local``.
    """

    user_id: int
    provider: str


class AuthenticationProvider(Protocol):
    """Define the credential-verification contract for login providers."""

    provider_id: str

    def authenticate(
        self, db: Session, credentials: dict[str, str]
    ) -> AuthenticationResult | None:
        """Verify credentials and resolve an existing LabRat account.

        Args:
            db: An active SQLAlchemy session.
            credentials: Credentials relevant to the provider.

        Returns:
            AuthenticationResult | None: Verified local identity, or ``None``.
        """
        ...


class LocalPasswordProvider:
    """Check local usernames and passwords against stored Argon2id hashes."""

    provider_id = "local"

    def __init__(self) -> None:
        """Initialize the Argon2id hasher and a hash for unknown accounts.

        The dummy hash lets a sign-in for an account that does not exist do
        the same Argon2 work as one for an account that does, so response
        time does not reveal which usernames exist.
        """
        self.hasher = PasswordHasher(type=Type.ID)
        self._dummy_hash = self.hasher.hash(secrets.token_urlsafe(32))

    def _spend_equal_time(self, password: str) -> None:
        """Verify against the dummy hash, which never matches.

        Args:
            password: The submitted password.
        """
        try:
            self.hasher.verify(self._dummy_hash, password)
        except (VerificationError, InvalidHashError):
            pass

    def authenticate(
        self, db: Session, credentials: dict[str, str]
    ) -> AuthenticationResult | None:
        """Authenticate an active local account, upgrading hashes if needed.

        This function never logs plaintext credentials. It modifies an obsolete
        hash in the supplied SQLAlchemy transaction; callers commit or roll back.

        Args:
            db: Transactional session containing user and credential records.
            credentials: Dictionary with ``username`` and ``password`` keys.

        Returns:
            AuthenticationResult | None: The resolved user, or ``None`` if
            verification fails or the user is inactive.
        """
        username = credentials.get("username", "")
        password = credentials.get("password", "")
        if not isinstance(username, str) or not isinstance(password, str):
            return None
        if len(username) > 120 or len(password) > 1024:
            return None

        username = username.strip().lower()
        user = db.scalar(
            select(User).where(User.username == username, User.is_active.is_(True))
        )
        credential = db.get(LocalCredential, user.id) if user is not None else None
        if user is None or credential is None:
            self._spend_equal_time(password)
            return None

        try:
            verified = self.hasher.verify(credential.password_hash, password)
        except (VerificationError, InvalidHashError):
            return None

        if not verified:
            return None
        if self.hasher.check_needs_rehash(credential.password_hash):
            credential.password_hash = self.hasher.hash(password)

        return AuthenticationResult(user_id=user.id, provider=self.provider_id)


class ProviderRegistry:
    """Collect credential providers behind one route-independent interface."""

    def __init__(self) -> None:
        """Register the built-in local password provider as a fallback."""
        self._providers: dict[str, AuthenticationProvider] = {}
        self.register(LocalPasswordProvider())

    def register(self, provider: AuthenticationProvider) -> None:
        """Register a provider under its unique identifier.

        Args:
            provider: Provider implementing ``AuthenticationProvider``.

        Raises:
            ValueError: A provider with the same identifier already exists.
        """
        if provider.provider_id in self._providers:
            raise ValueError(f"Authentication provider already registered: {provider.provider_id}")
        self._providers[provider.provider_id] = provider

    def get(self, provider_id: str) -> AuthenticationProvider:
        """Look up an installed authentication provider.

        Args:
            provider_id: Registered provider ID, for example ``local``.

        Returns:
            AuthenticationProvider: The selected provider.

        Raises:
            KeyError: The requested provider is not registered.
        """
        return self._providers[provider_id]
