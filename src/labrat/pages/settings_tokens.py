# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Let administrators create and revoke machine API tokens in Settings."""

from flask import Blueprint, redirect, render_template, request, url_for
from flask.typing import ResponseReturnValue

from labrat.api_tokens import EXPIRY_CHOICES, SCOPES, TokenError, create_token, list_tokens, revoke_token
from labrat.auth.session import login_required, require_admin_form, session_factory

tokens_bp = Blueprint("tokens", __name__, url_prefix="/settings/tokens")


def tokens_context() -> dict:
    """Gather what the API tokens panel shows.

    Returns:
        dict: The tokens (never their secrets), scopes, and expiry choices.
    """
    with session_factory()() as db:
        tokens = list_tokens(db)
    return {"api_tokens": tokens, "token_scopes": SCOPES, "token_expiry_choices": EXPIRY_CHOICES}



def _panel(status: int = 200, **extra: object) -> ResponseReturnValue:
    """Render Settings on its API tokens panel.

    Args:
        status: The HTTP status.
        **extra: A new token to show once, or an error.

    Returns:
        ResponseReturnValue: The page.
    """
    return render_template("private/settings.html", panel="tokens", **tokens_context(), **extra), status


@tokens_bp.post("")
@login_required
def create() -> ResponseReturnValue:
    """Create a token and show it once.

    The page answers this POST directly instead of redirecting, so the
    secret never passes through the session cookie or a URL.

    Returns:
        ResponseReturnValue: The panel with the new token, or the form error.
    """
    viewer = require_admin_form()
    try:
        with session_factory().begin() as db:
            token, secret = create_token(
                db, name=request.form.get("name", ""), scopes=request.form.getlist("scope"),
                expiry=request.form.get("expiry", ""), creator_id=viewer.id, creator_name=viewer.username,
            )
            name = token.name
    except TokenError as exc:
        return _panel(400, token_error=str(exc), token_form=request.form)
    response = render_template("private/settings.html", panel="tokens", **tokens_context(),
                               new_token=secret, new_token_name=name)
    return response, 201, {"Cache-Control": "no-store"}


@tokens_bp.post("/<int:token_id>/revoke")
@login_required
def revoke(token_id: int) -> ResponseReturnValue:
    """Revoke a token so it stops working at once.

    Args:
        token_id: The token's row id.

    Returns:
        ResponseReturnValue: A redirect back to the panel, or its error.
    """
    viewer = require_admin_form()
    try:
        with session_factory().begin() as db:
            revoke_token(db, token_id, actor_id=viewer.id, actor_name=viewer.username)
    except TokenError as exc:
        return _panel(400, token_error=str(exc))
    return redirect(url_for("private.settings", panel="tokens", saved="revoked"))
