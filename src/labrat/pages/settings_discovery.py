# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Serve administrator-only discovery controls and explicit scan permission forms."""

from flask import Blueprint, abort, current_app, redirect, render_template, request, url_for
from sqlalchemy.exc import SQLAlchemyError

from labrat.auth.session import current_user, login_required, require_csrf, session_factory
from labrat.discovery_settings import panel_context, save_settings
from labrat.integrations import IntegrationError


discovery_bp = Blueprint('discovery_settings', __name__, url_prefix='/settings/discovery')


def settings_context(*, submitted=None) -> dict:
    """Build whitelisted page/drawer context from fresh private settings and cached evidence.

    Args:
        submitted: The posted form when re-rendering after a failed save;
            None reads the query string.

    Returns:
        dict: The panel's template context.
    """
    fields = submitted if submitted is not None else request.args
    with session_factory()() as db:
        return panel_context(current_app.extensions['labrat_config_path'], db,
                             method=fields.get('method'), target=fields.get('target'),
                             subnet=fields.get('subnet'), submitted=submitted)


@discovery_bp.post('/save')
@login_required
def save():
    """Persist one explicit discovery change with CSRF, revision checks, and audit metadata.

    Returns:
        ResponseReturnValue: A redirect to the Discovery panel after saving,
            or the panel with the error.

    Raises:
        Forbidden: When the viewer is not an administrator (HTTP 403).
    """
    request.max_content_length = 32768
    viewer = current_user()
    if viewer is None or not viewer.is_admin:
        abort(403)
    require_csrf()
    try:
        save_settings(current_app.extensions['labrat_config_path'], request.form, session_factory(), viewer)
        return redirect(url_for('private.settings', panel='discovery', saved='discovery'))
    except IntegrationError as exc:
        message, status = str(exc), 400
    except (OSError, SQLAlchemyError):
        message, status = 'Discovery settings could not be saved. Check file access and try again.', 503
    try:
        context = settings_context(submitted=request.form)
    except IntegrationError:
        context = {}
    return render_template('private/settings.html', panel='discovery', discovery_error=message, **context), status
