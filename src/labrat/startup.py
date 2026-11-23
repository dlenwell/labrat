# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Serve a holding page until the web app is ready to take requests.

Gunicorn loads :class:`StartupGate` as ``labrat.app:app``. It answers at
once, so a container platform never shows a blank page or a dead port
while LabRat starts: each request gets the holding page
(``templates/startup/holding.html``) until a background thread has

1. loaded the configuration and built the Flask app (an error shows on
   the page, and the thread tries again, so fixing the config recovers
   without a restart);
2. taken the ``web`` lease (``labrat/instance.py``). A live lease held by
   another server is a conflict: the page names the holder, and a
   signed-in administrator chooses to take over, to stop this server, or
   to fork a copy of the database. Sign-in works meanwhile;
3. brought the schema to Alembic's head: migrated in place, under a file
   lock, when ``LABRAT_MIGRATE_ON_START`` (or ``[server].migrate_on_start``)
   is on, as the container image sets; otherwise the page says to run
   ``alembic upgrade head`` and checks again every few seconds.

Then every request goes to the Flask app. A renewal thread keeps the
lease; if another server takes it over, this one steps down and shows
the holding page again. ``/api/v1/live`` answers 200 whenever the process
does, for liveness probes; ``/api/v1/health`` answers 503 until the app
is ready.
"""

from collections.abc import Callable, Iterable
import json
import logging
import mimetypes
import os
from pathlib import Path
import queue
import signal
import sys
import threading
from typing import Any

from flask import Flask
from jinja2 import Environment, FileSystemLoader, select_autoescape
from werkzeug.exceptions import HTTPException
from werkzeug.security import safe_join
from werkzeug.wrappers import Request, Response

from labrat import app_version, instance
from labrat.config import load_local_config
from labrat.database import database_path, make_engine
from labrat.themes import ACCENT_COOKIE, DEFAULT_ACCENT, DEFAULT_THEME, THEME_COOKIE, chosen_accent, chosen_theme

LOG = logging.getLogger("labrat.startup")
PACKAGE = Path(__file__).resolve().parent
STATIC = PACKAGE / "static"

RETRY_SECONDS = 10
"""How long the thread waits before trying again after an error."""

SCHEMA_POLL_SECONDS = 5
"""How often a server waiting on a migration checks the schema again."""

DECISIONS = ("take-over", "refuse", "fork")
"""What an administrator may choose for a lease conflict."""

PASS_THROUGH = ("/signin", "/signout")
"""Paths the Flask app serves during a conflict, so an administrator can sign in."""

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    ),
}
"""The app's own browser protections, for the pages served before it exists."""


class MigrationFailed(RuntimeError):
    """Alembic exited with an error while migrating at startup."""



def migrate_on_start(config: dict[str, Any]) -> bool:
    """Say whether this server should migrate the database itself.

    Args:
        config: Parsed local configuration.

    Returns:
        bool: True when ``LABRAT_MIGRATE_ON_START`` is ``1``/``true`` or
            ``[server].migrate_on_start`` is true.
    """
    if os.environ.get("LABRAT_MIGRATE_ON_START", "").strip().lower() in {"1", "true", "yes"}:
        return True
    server = config.get("server", {})
    return isinstance(server, dict) and server.get("migrate_on_start") is True


def describe_error(error: BaseException) -> str:
    """Say what stopped startup without echoing anything sensitive.

    Configuration checks raise ``RuntimeError`` or ``ValueError`` with a
    message written for the reader (which setting, and how to fix it);
    anything else is named only by its type, and the full traceback goes
    to the server log.

    Args:
        error: The exception that stopped startup.

    Returns:
        str: One sentence for the holding page.
    """
    if isinstance(error, (MigrationFailed, RuntimeError, ValueError, TypeError)):
        return str(error)
    return f"{type(error).__name__} while starting; the server log has the details."


def fork_marker(config: dict[str, Any], holder: str) -> Path:
    """Name the file that tells every worker of a server it has forked.

    Args:
        config: Parsed local configuration.
        holder: The server's holder id.

    Returns:
        Path: ``<database>.fork-<holder>``, beside the database.
    """
    safe = "".join(char if char.isalnum() or char in "-_." else "_" for char in holder)
    return Path(f"{database_path(config)}.fork-{safe}")


def clear_fork_marker(holder: str) -> None:
    """Forget a fork when a server starts afresh (Gunicorn's ``on_starting``).

    A fork lasts for one server run: restarting returns to the configured
    database unless its path is changed to the copy.

    Args:
        holder: The starting server's holder id.
    """
    try:
        fork_marker(load_local_config(), holder).unlink(missing_ok=True)
    except (OSError, ValueError, TypeError):
        pass


def release_web_lease(holder: str) -> None:
    """Give up the web lease when the server exits (Gunicorn's ``on_exit``).

    Args:
        holder: The exiting server's holder id.
    """
    try:
        config = load_local_config()
        engine = make_engine(config)
        try:
            if instance.has_lease_table(engine):
                instance.release(engine, "web", holder)
        finally:
            engine.dispose()
    except Exception:  # Exiting anyway; never mask the shutdown.
        LOG.warning("Could not release the web lease on exit")


class StartupGate:
    """A WSGI app that holds requests until the Flask app is ready.

    Args:
        factory: Builds the Flask app; takes an optional database override
            for a forked server.
    """

    def __init__(self, factory: Callable[..., Flask]) -> None:
        """Prepare the gate; the work starts with :meth:`start` or the first request.

        Args:
            factory: ``create_app``, called with ``database_override`` for a fork.
        """
        self._factory = factory
        self._lock = threading.Lock()
        self._started = False
        self._state = "starting"
        self._view: dict[str, Any] = {}
        self._app: Flask | None = None
        self._auth_app: Flask | None = None
        self._decisions: queue.Queue[str] = queue.Queue()
        self._stopping = threading.Event()
        self._holder = ""
        self._server_pid = os.getpid()
        self._version = app_version()
        self._templates = Environment(
            loader=FileSystemLoader(str(PACKAGE / "templates")),
            autoescape=select_autoescape(["html"]),
        )

    # ---- State --------------------------------------------------------

    @property
    def state(self) -> str:
        """Name where startup stands.

        Returns:
            str: ``starting``, ``migrating``, ``waiting``, ``conflict``,
                ``error``, ``ready``, ``stepped_down``, or ``refused``.
        """
        return self._state

    def _set(self, state: str, **view: Any) -> None:
        """Move to a state and describe it for the holding page.

        Args:
            state: The new state.
            **view: Page text and details for it.
        """
        self._view = view
        self._state = state

    def start(self, server_pid: int | None = None) -> None:
        """Start the startup thread once (Gunicorn's ``post_worker_init``).

        Args:
            server_pid: The Gunicorn master's pid, shared by every worker so
                they hold one lease; defaults to the parent under Gunicorn.
        """
        with self._lock:
            if self._started:
                return
            self._started = True
            if server_pid is None:
                server_pid = os.getppid() if "gunicorn" in sys.modules else os.getpid()
            self._server_pid = server_pid
            self._holder = instance.holder_id(server_pid)
        threading.Thread(target=self._run, name="labrat-startup", daemon=True).start()

    def stop(self) -> None:
        """Stop the startup and renewal threads and close the apps' databases."""
        self._stopping.set()
        for app in {id(app): app for app in (self._app, self._auth_app) if app is not None}.values():
            app.extensions["labrat_engine"].dispose()

    # ---- Startup thread -----------------------------------------------

    def _run(self) -> None:
        """Bring the app up, retrying after errors, then keep the lease."""
        while not self._stopping.is_set():
            try:
                if self._bring_up():
                    return
            except Exception as error:  # Shown on the page, then retried.
                LOG.exception("LabRat could not start")
                self._set(
                    "error", eyebrow="Startup stopped", title="LabRat could not start",
                    message=describe_error(error),
                    hint=f"Fix the cause and LabRat tries again on its own every {RETRY_SECONDS} seconds.",
                    refresh=RETRY_SECONDS,
                )
                self._stopping.wait(RETRY_SECONDS)

    def _bring_up(self) -> bool:
        """Make one attempt at startup.

        Returns:
            bool: True once the app is serving (or this server stood down);
                False to try again from the top.
        """
        if self._state == "starting":
            self._set("starting", eyebrow="Starting up", title="LabRat is starting",
                      message="Loading the configuration and opening the database.", refresh=3)
        config = load_local_config()
        if self._auth_app is None or self._state == "error":
            # Built once and kept while waiting: it serves sign-in during a
            # conflict and becomes the app once ready.
            self._auth_app = None
            self._auth_app = self._factory()
        app = self._auth_app
        engine = make_engine(config)
        fork: Path | None = None
        serving = False
        try:
            if instance.has_lease_table(engine):
                outcome = self._claim(engine, config)
                if outcome == "refused":
                    return True
                if isinstance(outcome, Path):
                    fork = outcome
            if fork is not None:
                engine.dispose()
                forked = dict(config)
                forked["database"] = {**(config.get("database") or {}), "path": str(fork)}
                config = forked
                engine = make_engine(config)
            if not self._migrate(engine, config):
                return False
            if fork is not None or instance.has_lease_table(engine):
                # First lease after the migration that created the table, or
                # this fork's own lease in its copy.
                if instance.acquire(engine, "web", self._holder, self._version) is not None:
                    return False
            if fork is not None:
                app = self._factory(database_override=fork)
            serving = True
        finally:
            if not serving:
                engine.dispose()
        self._app = app
        self._set("ready")
        LOG.info("LabRat is ready%s", f" on a fork of the database ({fork.name})" if fork else "")
        threading.Thread(target=self._keep_lease, args=(engine, fork), name="labrat-lease", daemon=True).start()
        return True

    def _migrate(self, engine: Any, config: dict[str, Any]) -> bool:
        """Bring the schema to head, or wait for someone to.

        Args:
            engine: The database engine.
            config: Parsed local configuration.

        Returns:
            bool: True when the schema is current.

        Raises:
            MigrationFailed: Alembic exited with an error.
        """
        state = instance.schema_state(engine)
        if state is None or not state.behind:
            return True
        details = {"Database revision": state.current or "none (new database)", "This version needs": state.head or ""}
        if not migrate_on_start(config):
            self._set(
                "waiting", eyebrow="Waiting for the database", title="The database needs a migration",
                message="This version of LabRat needs a newer database schema than the one it found.",
                details=details, command="alembic -c alembic.ini upgrade head",
                hint=("Run this from the LabRat folder, or set LABRAT_MIGRATE_ON_START=1 to let the "
                      f"server do it. This page checks again every {SCHEMA_POLL_SECONDS} seconds."),
                refresh=SCHEMA_POLL_SECONDS,
            )
            self._stopping.wait(SCHEMA_POLL_SECONDS)
            return False
        self._set(
            "migrating", eyebrow="Upgrading", title="Migrating the database",
            message="LabRat is bringing the database up to date. Large databases can take a few minutes; nothing is lost if you wait.",
            details=details, hint="This page reloads itself and opens LabRat when it is done.", refresh=5,
        )
        with instance.migration_lock(config):
            state = instance.schema_state(engine)
            if state is not None and state.behind:
                LOG.info("Migrating the database from %s to %s", state.current, state.head)
                status = instance.run_migration()
                if status != 0:
                    raise MigrationFailed(
                        f"The database migration failed (Alembic exit status {status}). "
                        "The database is unchanged where the migration could roll back; the server log has Alembic's output."
                    )
        return True

    def _claim(self, engine: Any, config: dict[str, Any]) -> str | Path:
        """Take the web lease, or wait for a decision while another server holds it.

        Args:
            engine: The database engine.
            config: Parsed local configuration.

        Returns:
            str | Path: ``"ours"`` once we hold the lease, ``"refused"`` when
                this server stood down, or the forked database's path.
        """
        marker = fork_marker(config, self._holder)
        while True:
            if self._stopping.is_set():
                return "refused"
            if marker.is_file():
                forked = Path(marker.read_text(encoding="utf-8").strip())
                if forked.is_file():
                    return forked
            holder = instance.acquire(engine, "web", self._holder, self._version)
            if holder is None:
                return "ours"
            self._set(
                "conflict", eyebrow="Another LabRat is running", title="This database is already in use",
                message=("Another LabRat web server is using this database. Two servers writing one "
                         "SQLite database can double-poll sources and corrupt data, so this one is waiting."),
                details=holder.describe(), actions=True, refresh=10,
            )
            try:
                decision = self._decisions.get(timeout=2)
            except queue.Empty:
                continue
            if decision == "take-over":
                instance.take_over(engine, self._holder, self._version)
                LOG.warning("Took the web lease over from %s", holder.holder)
                return "ours"
            if decision == "refuse":
                self._set(
                    "refused", eyebrow="Stopped", title="This server stood down",
                    message="It left the database to the other LabRat and is shutting down.",
                    details=holder.describe(), refresh=0,
                )
                LOG.warning("Refused to start: %s holds the web lease", holder.holder)
                threading.Timer(1.0, os.kill, args=(self._server_pid, signal.SIGTERM)).start()
                return "refused"
            if decision == "fork":
                with instance.migration_lock(config):
                    if not marker.is_file():
                        forked = instance.fork_database(config, self._holder)
                        marker.write_text(str(forked), encoding="utf-8")
                        LOG.warning("Forked the database to %s", forked)

    def _keep_lease(self, engine: Any, fork: Path | None) -> None:
        """Renew the web lease until someone takes it over.

        Args:
            engine: The engine for the database this server runs on.
            fork: The forked database, if this server runs on a copy.
        """
        while not self._stopping.wait(instance.RENEW_SECONDS):
            try:
                if not instance.has_lease_table(engine):
                    continue
                current = instance.renew(engine, "web", self._holder)
            except Exception:  # A busy database; try at the next renewal.
                LOG.warning("Could not renew the web lease; trying again")
                continue
            if current is None:
                continue
            LOG.warning("Another server took the web lease over; this one is stepping down")
            details = current.describe() if current.holder and not current.holder.startswith("released-by:") else {}
            self._set(
                "stepped_down", eyebrow="Replaced", title="Another server took over",
                message=("An administrator on another LabRat server took this database over, so this "
                         "server has stopped serving it. Restart this server to use it again."),
                details=details, refresh=0,
            )
            break
        engine.dispose()

    # ---- Requests -----------------------------------------------------

    def __call__(self, environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        """Hand a request to the Flask app once ready; hold it until then.

        Args:
            environ: The WSGI environment.
            start_response: The WSGI response starter.

        Returns:
            Iterable[bytes]: The response body.
        """
        if not self._started:
            self.start()
        app = self._app
        if app is not None and self._state == "ready":
            return app(environ, start_response)
        return self._hold(Request(environ))(environ, start_response)

    def _hold(self, request: Request) -> Any:
        """Answer a request while the app is not serving.

        Args:
            request: The incoming request.

        Returns:
            Any: A WSGI response (the holding page, a probe answer, a
                static file, or the Flask app for sign-in).
        """
        path = request.path
        if path == "/api/v1/live":
            return self._json({"service": "labrat", "status": "live", "startup": self._state}, 200)
        if path == "/api/v1/health":
            return self._json({"service": "labrat", "status": self._state}, 503)
        if path.startswith("/static/"):
            return self._static(path.removeprefix("/static/"))
        auth = self._auth_app
        if self._state == "conflict" and auth is not None:
            if path in PASS_THROUGH:
                return auth
            if path == "/startup/decision" and request.method == "POST":
                return self._decide(auth, request)
        return self._page(request)

    def _json(self, body: dict[str, Any], status: int) -> Response:
        """Build a small JSON answer for probes.

        Args:
            body: The JSON document.
            status: The HTTP status.

        Returns:
            Response: The response.
        """
        response = Response(json.dumps(body), status=status, mimetype="application/json")
        response.headers.update(SECURITY_HEADERS)
        return response

    def _static(self, name: str) -> Response:
        """Serve a file from the app's static folder for the holding page.

        Args:
            name: The path under ``static/``.

        Returns:
            Response: The file, or 404.
        """
        joined = safe_join(str(STATIC), name)
        if joined is None or not Path(joined).is_file():
            return Response("Not found", status=404, mimetype="text/plain")
        kind = mimetypes.guess_type(joined)[0] or "application/octet-stream"
        response = Response(Path(joined).read_bytes(), mimetype=kind)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-cache"
        return response

    def _page(self, request: Request) -> Any:
        """Render the holding page for the current state.

        During a conflict it renders inside the Flask app's request, so it
        can read the signed-in account and give the form a CSRF token.

        Args:
            request: The incoming request.

        Returns:
            Any: A WSGI response with HTTP 503 and ``Retry-After``.
        """
        view = {
            "state": self._state, "eyebrow": "Starting up", "title": "LabRat is starting",
            "message": "", "details": {}, "command": "", "hint": "", "refresh": 3, "actions": False,
            "version": self._version, "renew_seconds": instance.RENEW_SECONDS,
            "theme": chosen_theme(request.cookies.get(THEME_COOKIE)) or DEFAULT_THEME,
            "accent": chosen_accent(request.cookies.get(ACCENT_COOKIE)) or DEFAULT_ACCENT,
            "signed_in_admin": False, "csrf_token": "", "signin_url": "/signin",
            **self._view,
        }
        auth = self._auth_app
        if view["actions"] and auth is not None:
            from labrat.auth.session import csrf_token, current_user

            with auth.request_context(request.environ):
                viewer = current_user()
                view["signed_in_admin"] = bool(viewer is not None and viewer.is_admin)
                view["csrf_token"] = csrf_token() if view["signed_in_admin"] else ""
                response = auth.make_response(self._render(view))
                response = auth.process_response(response)
        else:
            response = Response(self._render(view), mimetype="text/html")
        response.status_code = 503
        response.headers["Retry-After"] = str(view["refresh"] or RETRY_SECONDS)
        response.headers.update(SECURITY_HEADERS)
        return response

    def _render(self, view: dict[str, Any]) -> str:
        """Render the holding template.

        Args:
            view: Template variables.

        Returns:
            str: The page's HTML.
        """
        return self._templates.get_template("startup/holding.html").render(**view)

    def _decide(self, auth: Flask, request: Request) -> Any:
        """Accept an administrator's choice for a lease conflict.

        Args:
            auth: The Flask app, for the session and CSRF check.
            request: The form submission.

        Returns:
            Any: A redirect back to the holding page, or an error response.
        """
        from flask import request as form_request

        from labrat.audit import record_audit_event
        from labrat.auth.session import current_user, require_csrf

        with auth.request_context(request.environ):
            viewer = current_user()
            if viewer is None or not viewer.is_admin:
                return Response("Sign in as an administrator to choose.", status=403, mimetype="text/plain")
            try:
                require_csrf()
            except HTTPException as error:
                return error
            # Flask's request: the CSRF check has already read the body through it.
            decision = form_request.form.get("decision", "")
            if decision not in DECISIONS:
                return Response("Unknown choice.", status=400, mimetype="text/plain")
            holder = self._view.get("details", {})
            with auth.extensions["labrat_session_factory"]() as db:
                record_audit_event(
                    db, actor_kind="user", actor_id=viewer.id, actor_name=viewer.username,
                    action=f"instance.{decision}", subject_kind="instance_lease",
                    details={"role": "web", "other_host": holder.get("Host", ""), "other_version": holder.get("Version", "")},
                )
                db.commit()
        self._decisions.put(decision)
        response = Response("", status=303)
        response.headers["Location"] = "/"
        return response
