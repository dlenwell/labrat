# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Render public, credential-free guides from LabRat's current code contract.

The API reference reads Flask's registered routes and their docstrings at
request time so method and path listings stay aligned with the running app.
The conceptual guides explain the persistence and discovery boundaries
implemented by the models, collector, and placement API.
"""

from inspect import getdoc

from flask import Blueprint, current_app, render_template
from flask.typing import ResponseReturnValue


docs_bp = Blueprint("docs", __name__)


@docs_bp.get("/docs")
def index() -> ResponseReturnValue:
    """Render the documentation landing page.

    Returns:
        ResponseReturnValue: Public guide index with no private lab data.
    """
    return render_template("docs/index.html")


@docs_bp.get("/docs/setup")
def setup() -> ResponseReturnValue:
    """Walk through install, first-time setup, Settings, and the collector.

    Returns:
        ResponseReturnValue: Public setup guide with demo screenshots.
    """
    return render_template("docs/setup.html")


@docs_bp.get("/docs/tour")
def tour() -> ResponseReturnValue:
    """Show each page of the app, illustrated with the demo lab.

    Returns:
        ResponseReturnValue: Public page tour with demo screenshots.
    """
    return render_template("docs/tour.html")


@docs_bp.get("/docs/inventory")
def inventory() -> ResponseReturnValue:
    """Explain canonical inventory, placement, and link provenance.

    Returns:
        ResponseReturnValue: Public inventory model guide.
    """
    return render_template("docs/inventory.html")


@docs_bp.get("/docs/discovery")
def discovery() -> ResponseReturnValue:
    """Explain configured source collection and stored observations.

    Returns:
        ResponseReturnValue: Public discovery guide.
    """
    return render_template("docs/discovery.html")


@docs_bp.get("/docs/api")
def api() -> ResponseReturnValue:
    """List live JSON routes using registered methods and view docstrings.

    Returns:
        ResponseReturnValue: Public API reference without credentials or data.
    """
    routes: list[dict[str, str]] = []
    for rule in current_app.url_map.iter_rules():
        if not rule.rule.startswith("/api/"):
            continue
        view = current_app.view_functions[rule.endpoint]
        summary = (getdoc(view) or "").splitlines()[0]
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            access = (
                "Public" if rule.endpoint == "api.health"
                else "Admin + CSRF" if method not in {"GET", "HEAD"}
                else "Signed-in"
            )
            routes.append({
                "method": method,
                "path": rule.rule,
                "summary": summary,
                "access": access,
            })
    routes.sort(key=lambda item: (item["path"], item["method"]))
    return render_template("docs/api.html", routes=routes)
