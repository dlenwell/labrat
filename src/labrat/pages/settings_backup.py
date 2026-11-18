# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Serve the administrator-only whole-install export and its replacing import."""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from flask import Blueprint, abort, current_app, make_response, redirect, render_template, request, session, url_for
from flask.typing import ResponseReturnValue
from werkzeug.exceptions import RequestEntityTooLarge

from labrat.audit import record_audit_event
from labrat.auth.session import current_user, login_required, require_csrf, session_factory
from labrat.database import database_path
from labrat.instance_transfer import MAX_UPLOAD_BYTES, TransferError, export_instance, import_instance, read_instance

backup_bp = Blueprint("backup", __name__, url_prefix="/settings/backup")


def _admin() -> object:
    """Require a current administrator and a valid form CSRF token.

    Returns:
        object: The signed-in administrator.

    Raises:
        Forbidden: When the viewer is not an administrator (HTTP 403).
    """
    viewer = current_user()
    if viewer is None or not viewer.is_admin:
        abort(403)
    require_csrf()
    return viewer


def _database() -> Path:
    """Return the configured SQLite path without exposing local configuration.

    Returns:
        Path: The database file's path.
    """
    return database_path(current_app.extensions["labrat_local_config"])


def _app_version() -> str:
    """Return this LabRat's version, or ``dev`` from a source tree.

    Returns:
        str: The version.
    """
    try:
        return version("labrat")
    except PackageNotFoundError:
        return "dev"


def _page(error: str, status: int) -> ResponseReturnValue:
    """Render the Backup panel with a safe error.

    Args:
        error: The message to show.
        status: The HTTP status for the reply.

    Returns:
        ResponseReturnValue: The Settings page on its Backup panel.
    """
    return render_template("private/settings.html", panel="backup", error=error), status


@backup_bp.post("/export")
@login_required
def download() -> ResponseReturnValue:
    """Download the whole install, credentials included, as one ZIP file.

    Returns:
        ResponseReturnValue: The export as a file download.
    """
    viewer = _admin()
    with session_factory().begin() as db:
        record_audit_event(db, actor_kind="user", actor_id=viewer.id, actor_name=viewer.username,
                           action="instance.exported", subject_kind="instance", details={})
    try:
        data, manifest = export_instance(_database(), current_app.extensions["labrat_config_path"], _app_version())
    except TransferError as exc:
        return _page(str(exc), 500)
    response = make_response(data)
    response.mimetype = "application/zip"
    response.headers["Content-Disposition"] = f'attachment; filename="labrat-export-{manifest["created_at"][:10]}.zip"'
    response.headers["Cache-Control"] = "no-store"
    return response


@backup_bp.post("/import")
@login_required
def restore() -> ResponseReturnValue:
    """Replace this install with an uploaded export, then sign everyone out.

    The import replaces the accounts too, so the administrator signs in again
    with the old install's credentials.

    Returns:
        ResponseReturnValue: A redirect to sign in, or the Backup panel with
            the reason the import was refused.
    """
    request.max_content_length = MAX_UPLOAD_BYTES
    try:
        viewer = _admin()
        if request.form.get("confirm") != "replace":
            raise TransferError("Confirm that this import replaces everything in this LabRat.")
        upload = request.files.get("export")
        if upload is None or not upload.filename:
            raise TransferError("Choose a LabRat export file to import.")
        transfer = read_instance(upload.stream.read(MAX_UPLOAD_BYTES + 1))
        import_instance(_database(), current_app.extensions["labrat_config_path"], transfer)
    except RequestEntityTooLarge:
        return _page("Choose an export smaller than 256 MB.", 413)
    except TransferError as exc:
        return _page(str(exc), 400)
    with session_factory().begin() as db:
        record_audit_event(db, actor_kind="user", actor_name=viewer.username,
                           action="instance.imported", subject_kind="instance",
                           details={"created_at": transfer.manifest.get("created_at"),
                                    "app_version": transfer.manifest.get("app_version")})
    session.clear()
    return redirect(url_for("public.signin", imported=1))
