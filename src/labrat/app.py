# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Create LabRat's Flask application, authentication, and Gunicorn entry point.

Application startup loads local credentials, configures database access, and
registers independent blueprints for public HTML, private HTML, and JSON APIs.
Database schema changes remain Alembic's responsibility, not Flask's.
"""

from importlib.metadata import PackageNotFoundError, version
import sys
from typing import Any

from flask import Flask, Response
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from labrat.api.routes import api_bp
from labrat.api.locations import locations_bp
from labrat.api.observation_actions import observation_actions_bp
from labrat.api.rack_devices import rack_devices_bp
from labrat.api.topology import topology_bp
from labrat.api.clients import clients_bp
from labrat.api.cluster import cluster_bp
from labrat.api.network import network_bp
from labrat.auth.providers import ProviderRegistry
from labrat.auth.session import csrf_token, current_user
from labrat.auth.setup_code import ensure_setup_code, remove_setup_code, setup_code_path
from labrat.auth.throttle import SignInThrottle
from labrat.pages.navigation import location_menu
from labrat.themes import current_accent, current_theme
from labrat.config import load_local_config, local_config_path
from labrat.database import database_path, make_engine, make_session_factory
from labrat.pages.private import private_bp
from labrat.pages.public import public_bp
from labrat.pages.status import header_sources, source_health
from labrat.pages.docs import docs_bp
from labrat.models import User


LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
"""Hosts that only this machine can reach."""

MAX_REQUEST_BYTES = 256 * 1024
"""Largest request body any route accepts unless it raises its own limit."""


def create_app() -> Flask:
    """Create the LabRat Flask application with secure local session settings.

    Returns:
        Flask: Configured application with session, database, and route services.

    Raises:
        RuntimeError: The local session secret is missing or too short.
        ValueError: Auth settings have an invalid type.
        OSError: The configured database directory cannot be created.
    """
    app = Flask(__name__)
    local_config = load_local_config()
    app.extensions["labrat_local_config"] = local_config
    app.extensions["labrat_config_path"] = local_config_path().resolve()

    server_settings: Any = local_config.get("server", {})
    if not isinstance(server_settings, dict):
        raise ValueError("[server] must be a TOML table")
    allowed_hosts = server_settings.get(
        "allowed_hosts", ["localhost", "127.0.0.1", "::1"]
    )
    if (
        not isinstance(allowed_hosts, list)
        or not allowed_hosts
        or any(
            not isinstance(host, str)
            or not host.strip()
            or host.strip() != host
            or "/" in host
            for host in allowed_hosts
        )
    ):
        raise ValueError(
            "[server].allowed_hosts must be a nonempty list of hostnames or IP addresses"
        )

    auth_settings: Any = local_config.get("auth", {})
    if not isinstance(auth_settings, dict):
        raise ValueError("[auth] must be a TOML table")
    secret = auth_settings.get("secret_key")
    if (
        not isinstance(secret, str)
        or len(secret) < 32
        or secret.startswith("REPLACE_")
    ):
        raise RuntimeError(
            "Set [auth] secret_key in local_config.toml to a random string of "
            "at least 32 characters. Generate one with: "
            "python -c 'import secrets; print(secrets.token_urlsafe(48))'"
        )
    secure_cookies = auth_settings.get("secure_cookies", False)
    if not isinstance(secure_cookies, bool):
        raise ValueError("[auth].secure_cookies must be true or false")
    remote_hosts = [host for host in allowed_hosts if host not in LOOPBACK_HOSTS]
    if remote_hosts and not secure_cookies:
        print(
            f"LabRat: [auth] secure_cookies is off while other machines may connect "
            f"({', '.join(remote_hosts)}). That is right for plain HTTP; once LabRat "
            "is served over HTTPS, set secure_cookies = true so the session cookie "
            "is never sent unencrypted.",
            file=sys.stderr, flush=True,
        )

    app.config.update(
        SECRET_KEY=secret,
        TRUSTED_HOSTS=allowed_hosts,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=secure_cookies,
        PERMANENT_SESSION_LIFETIME=3600 * 12,
        # Requests larger than this get HTTP 413 before any route reads them.
        # The backup upload raises its own limit for the request it handles.
        MAX_CONTENT_LENGTH=MAX_REQUEST_BYTES,
    )

    engine = make_engine(local_config)
    app.extensions["labrat_engine"] = engine
    app.extensions["labrat_setup_code_path"] = setup_code_path(database_path(local_config))
    _prepare_setup_code(app)
    app.extensions["labrat_session_factory"] = make_session_factory(engine)
    app.extensions["labrat_auth_providers"] = ProviderRegistry()
    app.extensions["labrat_signin_throttle"] = SignInThrottle()
    try:
        app_version = version("labrat")
    except PackageNotFoundError:  # Running from a source tree without install.
        app_version = "dev"

    @app.context_processor
    def template_globals() -> dict[str, Any]:
        """Expose safe viewer details, a CSRF token, and source health.

        Signed-in pages show one status light per discovery source in the
        header, so ``source_status`` is read from the local database for
        authenticated viewers only. Anonymous pages get an empty list.

        Returns:
            dict[str, Any]: Template variables for navigation (including the
                Locations menu's sites and rooms), POST forms,
                the header status strip (``source_strip``), the footer version, and this
                browser's theme (``data-theme`` on every page).
        """
        viewer = current_user()
        source_status: list[dict[str, Any]] = []
        locations_menu: list[dict[str, Any]] = []
        if viewer is not None:
            with app.extensions["labrat_session_factory"]() as db:
                source_status = source_health(db)
                locations_menu = location_menu(db)
        return {
            "viewer": viewer,
            "csrf_token": csrf_token(),
            "source_status": source_status,
            "source_strip": header_sources(source_status),
            "locations_menu": locations_menu,
            "app_version": app_version,
            "theme": current_theme(),
            "accent": current_accent(),
        }

    @app.after_request
    def security_headers(response: Response) -> Response:
        """Set conservative browser protections without inline JS or CSS.

        Args:
            response: Outgoing Flask response.

        Returns:
            Response: Response with baseline security headers applied.
        """
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; object-src 'none'; base-uri 'self'; "
            "form-action 'self'; frame-ancestors 'none'"
        )
        if response.mimetype == "text/html" or response.mimetype == "application/json":
            response.headers["Cache-Control"] = "no-store"
        return response

    app.register_blueprint(public_bp)
    app.register_blueprint(docs_bp)
    app.register_blueprint(private_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(topology_bp)
    app.register_blueprint(locations_bp)
    app.register_blueprint(rack_devices_bp)
    app.register_blueprint(observation_actions_bp)
    app.register_blueprint(clients_bp)
    app.register_blueprint(cluster_bp)
    app.register_blueprint(network_bp)
    return app


def _prepare_setup_code(app: Flask) -> None:
    """Create and print the first-run setup code, or remove a stale one.

    Args:
        app: The application being created.
    """
    path = app.extensions["labrat_setup_code_path"]
    try:
        with app.extensions["labrat_engine"].connect() as connection:
            has_account = connection.execute(select(User.id).limit(1)).first() is not None
    except SQLAlchemyError:
        # Not migrated yet: setup cannot run, and the setup page makes the
        # code when it first opens after migration.
        return
    if has_account:
        remove_setup_code(path)
    else:
        ensure_setup_code(path)


# Gunicorn loads this WSGI application with: gunicorn labrat.app:app
app = create_app()
