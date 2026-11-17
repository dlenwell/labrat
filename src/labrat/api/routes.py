# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Expose LabRat's independent, versioned JSON API under ``/api``.

Public health checks remain accessible anonymously. Protected endpoints use
the same session identity as HTML pages but return JSON HTTP 401, not redirects.
"""

from flask import Blueprint, jsonify
from flask.typing import ResponseReturnValue

from labrat.auth.session import api_login_required, current_user

api_bp = Blueprint("api", __name__, url_prefix="/api")


@api_bp.get("/v1/health")
def health() -> ResponseReturnValue:
    """Report Flask API liveness without probing collectors or storage.

    Returns:
        ResponseReturnValue: HTTP 200 with service name and liveness status.
    """
    return jsonify(service="labrat", status="ok")


@api_bp.get("/v1/me")
@api_login_required
def me() -> ResponseReturnValue:
    """Return the current user's safe profile fields as JSON.

    Returns:
        ResponseReturnValue: User ID, username, display name, and admin flag.
    """
    user = current_user()
    assert user is not None
    return jsonify(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        is_admin=user.is_admin,
    )
