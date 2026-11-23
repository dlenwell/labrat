# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Serve public pages, first-run administrator setup, and local sign-in.

Initial setup is available only while the user table is empty and only through
a host listed in the local ``[server].allowed_hosts`` setting. Public signup is
intentionally disabled. HTML forms use session CSRF tokens; credentials are
never included in API responses or logs.
"""

import re

from argon2 import PasswordHasher, Type
from flask import (
    Blueprint,
    abort,
    current_app,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask.typing import ResponseReturnValue
from sqlalchemy import select

from labrat.auth.session import (
    current_user,
    end_all_sessions,
    has_users,
    require_csrf,
    session_factory,
    sign_in,
)
from labrat.audit import record_audit_event
from labrat.auth.providers import new_password_problem
from labrat.auth.setup_code import ensure_setup_code, remove_setup_code, setup_code_matches
from labrat.auth.throttle import throttle_keys
from labrat.models import AuthIdentity, LocalCredential, User

public_bp = Blueprint("public", __name__)
_USERNAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,63}\Z")


def _validate_admin_form(username: str, password: str, confirm: str) -> str | None:
    """Validate first-run credentials without persisting or logging a password.

    Args:
        username: Proposed local account name.
        password: Proposed plaintext password.
        confirm: Repeated plaintext password.

    Returns:
        str | None: Human-readable error, or ``None`` for valid input.
    """
    if not _USERNAME_PATTERN.fullmatch(username):
        return "Use 3–64 characters: letters, numbers, dots, hyphens, or underscores."
    return new_password_problem(password, confirm)


@public_bp.get("/")
def index() -> ResponseReturnValue:
    """Render the landing page with setup, sign-in, or home navigation.

    Returns:
        ResponseReturnValue: Rendered public HTML page.
    """
    return render_template("public/index.html", needs_setup=not has_users())


@public_bp.route("/setup", methods=["GET", "POST"])
def setup() -> ResponseReturnValue:
    """Create the first local administrator through a trusted host.

    This one-time operation takes a SQLite ``BEGIN IMMEDIATE`` write lock before
    checking whether any users exist, so concurrent submissions cannot create
    multiple initial administrators. Flask rejects requests whose host is not
    listed in ``[server].allowed_hosts``. It never creates database tables.

    Returns:
        ResponseReturnValue: Setup form, redirect to home, or 404 after setup.
    """
    if has_users():
        abort(404)

    code_path = current_app.extensions["labrat_setup_code_path"]
    ensure_setup_code(code_path)
    error = None
    username = ""
    if request.method == "POST":
        require_csrf()
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        confirmation = request.form.get("confirm_password", "")
        if not setup_code_matches(code_path, request.form.get("setup_code", "")):
            error = ("That setup code is not right. LabRat prints it to its log when it "
                     "starts, and keeps it in the .setup-code file beside its database.")
        else:
            error = _validate_admin_form(username, password, confirmation)
        if error is None:
            # Argon2id is deliberately computed outside the SQLite write lock.
            password_hash = PasswordHasher(type=Type.ID).hash(password)
            with session_factory()() as db:
                try:
                    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
                    if db.scalar(select(User.id).limit(1)) is not None:
                        abort(404)
                    user = User(username=username, display_name=username, is_admin=True)
                    db.add(user)
                    db.flush()
                    db.add_all([
                        AuthIdentity(user_id=user.id, provider="local", subject=username),
                        LocalCredential(user_id=user.id, password_hash=password_hash),
                    ])
                    db.commit()
                    user_id = user.id
                except Exception:
                    db.rollback()
                    raise
            remove_setup_code(code_path)
            sign_in(user_id)
            return redirect(url_for("private.home"))

    return render_template("public/setup.html", error=error, username=username)


@public_bp.route("/signin", methods=["GET", "POST"])
def signin() -> ResponseReturnValue:
    """Authenticate an existing local user and start a new signed session.

    Returns:
        ResponseReturnValue: Sign-in form or redirect to the private home page.
    """
    if not has_users():
        return redirect(url_for("public.setup"))
    if current_user() is not None:
        return redirect(url_for("private.home"))

    error = None
    username = ""
    if request.method == "POST":
        require_csrf()
        username = request.form.get("username", "").strip().lower()[:120]
        password = request.form.get("password", "")
        throttle = current_app.extensions["labrat_signin_throttle"]
        keys = throttle_keys(username, request.remote_addr)
        if throttle.blocked(keys):
            # Refused without checking the password, so guessing gains nothing.
            error = "Too many failed sign-ins. Wait a few minutes, then try again."
            return render_template("public/signin.html", error=error, username=username), 429
        registry = current_app.extensions["labrat_auth_providers"]
        with session_factory().begin() as db:
            result = registry.get("local").authenticate(
                db, {"username": username, "password": password}
            )
            if result is None and throttle.record_failure(keys):
                record_audit_event(
                    db, actor_kind="system", action="auth.signin.throttled",
                    subject_kind="user",
                    details={"username": username, "failures": throttle.limit,
                             "window_minutes": int(throttle.window // 60)},
                )
        if result is not None:
            throttle.clear(keys)
            sign_in(result.user_id)
            return redirect(url_for("private.home"))
        error = "Incorrect username or password."

    return render_template("public/signin.html", error=error, username=username)


@public_bp.post("/signout")
def signout() -> ResponseReturnValue:
    """End the account's sessions after validating the CSRF token.

    Returns:
        ResponseReturnValue: Redirect to the public landing page.
    """
    require_csrf()
    user_id = session.get("user_id")
    if type(user_id) is int:
        # Ends this account's sessions everywhere, so a copied cookie stops
        # working too, not only this browser's.
        end_all_sessions(user_id)
    session.clear()
    return redirect(url_for("public.index"))
