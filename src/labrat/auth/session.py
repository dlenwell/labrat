# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Manage signed Flask sessions and route-level authentication checks.

LabRat stores only a local user ID, the account's session version, and a
random CSRF token in Flask's signed, HTTP-only session cookie. It never stores
passwords, hashes, or adapter secrets in browser sessions. API routes return JSON 401; HTML routes redirect to sign-in.

Used by: api.clients, api.cluster, api.common, api.locations, api.network,
api.observation_actions, api.rack_devices, api.routes, api.topology, app,
pages.private, pages.public, pages.settings_backup,
pages.settings_discovery, pages.settings_integrations.
"""

import hmac
import secrets
from functools import wraps
from typing import Any, Callable, TypeVar

from flask import abort, current_app, jsonify, redirect, request, session, url_for
from flask.typing import ResponseReturnValue
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from labrat.models import User

RouteFunction = TypeVar("RouteFunction", bound=Callable[..., ResponseReturnValue])


def session_factory() -> sessionmaker:
    """Return this application's configured SQLAlchemy session factory.

    Returns:
        sessionmaker: Factory for short-lived SQLite-backed sessions.
    """
    return current_app.extensions["labrat_session_factory"]


def has_users() -> bool:
    """Report whether initial account setup has already been completed.

    Returns:
        bool: ``True`` when at least one user account exists.
    """
    with session_factory()() as db:
        return db.scalar(select(User.id).limit(1)) is not None


def current_user() -> User | None:
    """Resolve the active LabRat user referenced by the signed session.

    Deactivated and deleted accounts lose access on their next request, and
    so does a session started before the account's sessions were ended.

    Returns:
        User | None: An active account, or ``None`` for anonymous requests.
    """
    user_id = session.get("user_id")
    if type(user_id) is not int:
        return None

    with session_factory()() as db:
        user = db.get(User, user_id)
        if user is None or not user.is_active or session.get("session_version") != user.session_version:
            session.clear()
            return None
        return user


def csrf_token() -> str:
    """Get or create the unpredictable token used by HTML POST forms.

    Returns:
        str: Per-session CSRF token to include in a hidden form field.
    """
    token = session.get("csrf_token")
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def require_csrf() -> None:
    """Reject a submitted HTML form with a missing or invalid CSRF token.

    Raises:
        werkzeug.exceptions.BadRequest: The token is missing or incorrect.
    """
    expected = session.get("csrf_token", "")
    received = request.form.get("_csrf_token", "")
    if not (
        isinstance(expected, str)
        and expected
        and isinstance(received, str)
        and hmac.compare_digest(expected, received)
    ):
        abort(400, description="Invalid or expired form token. Reload the page and try again.")


def sign_in(user_id: int) -> None:
    """Rotate the session and store a newly authenticated user's ID.

    The session also records the account's session version, so ending the
    account's sessions later invalidates this one too.

    Args:
        user_id: Primary key of the successfully authenticated user.
    """
    with session_factory()() as db:
        version = db.scalar(select(User.session_version).where(User.id == user_id))
    session.clear()
    session["user_id"] = user_id
    session["session_version"] = version
    session["csrf_token"] = secrets.token_urlsafe(32)


def end_all_sessions(user_id: int) -> None:
    """End every session of an account, on every browser, and this one.

    Sessions are signed cookies the server cannot delete, so this bumps the
    account's session version; a session carrying an older one is refused.

    Args:
        user_id: The account whose sessions end.
    """
    with session_factory().begin() as db:
        user = db.get(User, user_id)
        if user is not None:
            user.session_version += 1
    session.clear()


def login_required(view: RouteFunction) -> RouteFunction:
    """Redirect unauthenticated HTML requests to the sign-in page.

    Args:
        view: HTML route handler requiring an active account.

    Returns:
        RouteFunction: Wrapped route enforcing an authenticated session.
    """

    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> ResponseReturnValue:
        """Check the current session before invoking the HTML handler."""
        if current_user() is None:
            return redirect(url_for("public.signin"))
        return view(*args, **kwargs)

    return wrapped  # type: ignore[return-value]


def api_login_required(view: RouteFunction) -> RouteFunction:
    """Return HTTP 401 JSON for unauthenticated protected API requests.

    Args:
        view: JSON API route handler requiring an active account.

    Returns:
        RouteFunction: Wrapped route enforcing an authenticated session.
    """

    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> ResponseReturnValue:
        """Check the current session before invoking the JSON API handler."""
        if current_user() is None:
            return jsonify(error="authentication_required"), 401
        return view(*args, **kwargs)

    return wrapped  # type: ignore[return-value]
