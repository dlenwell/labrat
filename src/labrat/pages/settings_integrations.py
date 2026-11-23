# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Serve administrator-only integration configuration and explicit API tests."""

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, url_for
from flask.typing import ResponseReturnValue
from sqlalchemy.exc import SQLAlchemyError

from labrat.auth.session import login_required, require_admin_form, session_factory
from labrat.integrations import (
    IntegrationError, answer_identity, candidate, panel_context, read_settings, save_integration, delete_integration,
    test_connection,
)

FORM_LIMIT = 32768
"""Largest configuration form accepted, in bytes."""

integrations_bp = Blueprint("integrations", __name__, url_prefix="/settings/integrations")


def settings_context(*, submitted: object | None = None) -> dict:
    """Read safe integration overview/editor data for the current request.

    Args:
        submitted: The posted form when re-rendering after a failed action;
            None reads the query string.

    Returns:
        dict: The panel's template context.
    """
    fields = submitted if submitted is not None else request.args
    kind = fields.get("kind")
    name = fields.get("name")
    is_new = fields.get("is_new") == "1"
    with session_factory()() as db:
        return panel_context(current_app.extensions["labrat_config_path"], db,
                             kind=kind, name=name, is_new=is_new, removing=fields.get("action") == "delete", submitted=submitted)



def _result(message: str, *, ok: bool = False, status: int = 400) -> ResponseReturnValue:
    """Return a safe test result or form page, never submitted secret fields.

    Args:
        message: The safe message to show.
        ok: Whether the action succeeded.
        status: The HTTP status for the reply.

    Returns:
        ResponseReturnValue: JSON for a script's request, else the Settings
            page with the message.
    """
    if request.accept_mimetypes.best == "application/json":
        return jsonify(ok=ok, message=message), status
    try:
        context = settings_context(submitted=request.form)
    except IntegrationError:
        context = {}
    return render_template("private/settings.html", panel="integrations",
                           integration_message=message, integration_ok=ok, **context), status


@integrations_bp.post("/save")
@login_required
def save() -> ResponseReturnValue:
    """Persist one connection in private TOML and audit source metadata only.

    Returns:
        ResponseReturnValue: A redirect to the Integrations panel after saving,
            or the panel with the error.
    """
    viewer = require_admin_form(FORM_LIMIT)
    try:
        kind, name = save_integration(current_app.extensions["labrat_config_path"],
                                      request.form, session_factory(), viewer)
        return redirect(url_for("private.settings", panel="integrations", saved="integration"))
    except IntegrationError as exc:
        return _result(str(exc))
    except (OSError, SQLAlchemyError):
        return _result("The integration could not be saved. Check configuration-file access and try again.", status=503)


@integrations_bp.post("/test")
@login_required
def test() -> ResponseReturnValue:
    """Test submitted credentials with a single read-only request; save nothing.

    Returns:
        ResponseReturnValue: The test's safe result message.
    """
    require_admin_form(FORM_LIMIT)
    try:
        _raw, config, revision = read_settings(current_app.extensions["labrat_config_path"])
        if request.form.get("revision") != revision:
            raise IntegrationError("Configuration changed. Reload the integration before testing.")
        kind, _name, settings, _new = candidate(request.form, config)
        ok, message = test_connection(kind, settings)
        return _result(message + " This test does not save changes.", ok=ok, status=200 if ok else 400)
    except IntegrationError as exc:
        return _result(str(exc))


@integrations_bp.post("/identity")
@login_required
def identity() -> ResponseReturnValue:
    """Answer a held poll: the same device at this address, or a different one.

    Returns:
        ResponseReturnValue: A redirect to the integration that collects
            next, or the panel with the error.
    """
    viewer = require_admin_form(FORM_LIMIT)
    try:
        kind, name = answer_identity(current_app.extensions["labrat_config_path"], request.form, session_factory(), viewer)
        return redirect(url_for("private.settings", panel="integrations", kind=kind, name=name, saved="identity"))
    except IntegrationError as exc:
        return _result(str(exc))
    except (OSError, SQLAlchemyError):
        return _result("The answer could not be saved. Check configuration-file access and try again.", status=503)


@integrations_bp.post("/delete")
@login_required
def remove() -> ResponseReturnValue:
    """Delete a saved connection after confirmation; retain inventory and observations.

    Returns:
        ResponseReturnValue: A redirect to the Integrations panel after the
            delete, or the panel with the error.
    """
    viewer = require_admin_form(FORM_LIMIT)
    try:
        delete_integration(current_app.extensions["labrat_config_path"], request.form, session_factory(), viewer)
        return redirect(url_for("private.settings", panel="integrations", saved="removed"))
    except IntegrationError as exc:
        return _result(str(exc))
    except (OSError, SQLAlchemyError):
        return _result("The integration could not be deleted. Check configuration-file access and try again.", status=503)
