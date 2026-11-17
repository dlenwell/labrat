# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Serve administrator-only inventory exports and explicit previewed restores."""

import hashlib
import os
import secrets
import time
from pathlib import Path

from flask import Blueprint, abort, current_app, make_response, redirect, render_template, request, session, url_for
from flask.typing import ResponseReturnValue
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.exc import IntegrityError, OperationalError
from werkzeug.exceptions import RequestEntityTooLarge

from labrat.audit import record_audit_event
from labrat.auth.session import current_user, login_required, require_csrf, session_factory
from labrat.database import database_path
from labrat.inventory_backup import (
    BackupError, MAX_BYTES, adopt_instance, canonical, decode_backup, export_backup, fingerprint,
    instance_id, restore_backup,
)

backup_bp = Blueprint("backup", __name__, url_prefix="/settings/backup")
PREVIEW_SECONDS = 15 * 60


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


def _preview_directory() -> Path:
    """Create a private upload directory and expire abandoned restore previews.

    Returns:
        Path: The preview directory, beside the database.
    """
    directory = _database().with_name(_database().name + ".restore-previews")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path in directory.glob("*.json"):
        try:
            if path.stat().st_mtime < time.time() - PREVIEW_SECONDS:
                path.unlink(missing_ok=True)
        except FileNotFoundError:
            continue
    return directory


def _signer() -> URLSafeTimedSerializer:
    """Bind opaque preview references to this application's signing secret.

    Returns:
        URLSafeTimedSerializer: The signer for restore tokens.
    """
    return URLSafeTimedSerializer(current_app.secret_key, salt="inventory-restore-v1")


def _session_binding() -> str:
    """Bind a restore token to the exact signed-in browser session.

    Returns:
        str: A hash of the session's CSRF token.
    """
    return hashlib.sha256(session["csrf_token"].encode()).hexdigest()


def _page(*, error: str | None = None, preview: dict | None = None, token: str | None = None, created_at: str | None = None, status: int = 200) -> ResponseReturnValue:
    """Render the backup panel with safe error or preview context.

    Args:
        error: A safe error message to show.
        preview: The validated backup's summary, before a restore.
        token: The signed reference that confirms that preview.
        created_at: When the uploaded backup was made.
        status: The HTTP status for the reply.

    Returns:
        ResponseReturnValue: The Settings page on its Backup panel.
    """
    return render_template("private/settings.html", panel="backup", error=error,
                           preview=preview, restore_token=token, created_at=created_at), status


def _audit(db: object, viewer: object, action: str, details: dict) -> None:
    """Record selected counts and intent, never uploaded inventory content.

    Args:
        db: The open write session.
        viewer: The administrator acting.
        action: The audit action, such as ``inventory_backup.restored``.
        details: The counts and intent to record.
    """
    record_audit_event(db, actor_kind="user", actor_id=viewer.id,
                       actor_name=viewer.username, action=action,
                       subject_kind="inventory_backup", details=details)


@backup_bp.post("/export")
@login_required
def download() -> ResponseReturnValue:
    """Download a no-store JSON inventory backup without accounts or secrets.

    Returns:
        ResponseReturnValue: The backup as a JSON file download.
    """
    viewer = _admin()
    with session_factory().begin() as db:
        # SQLite's explicit transaction gives all export queries one read snapshot.
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        data = export_backup(db, instance_id(_database()))
        _audit(db, viewer, "inventory.backup.exported", {
            "version": data["version"], "equipment": len(data["tables"]["devices"]),
        })
    response = make_response(canonical(data) + "\n")
    response.mimetype = "application/json"
    date = data["created_at"][:10]
    response.headers["Content-Disposition"] = f'attachment; filename="labrat-inventory-{date}.json"'
    response.headers["Cache-Control"] = "no-store"
    return response


@backup_bp.post("/preview")
@login_required
def preview() -> ResponseReturnValue:
    """Validate an upload by applying it inside a rolled-back savepoint.

    Only a random private file reference and a signed state fingerprint go
    to the browser. Uploaded values never enter the session cookie or logs.

    Returns:
        ResponseReturnValue: The Backup panel with the preview and a restore
            token, or with the reason the upload was refused.
    """
    request.max_content_length = MAX_BYTES + 65536
    try:
        viewer = _admin()
        upload = request.files.get("backup")
        if upload is None or not upload.filename:
            raise BackupError("Choose a LabRat inventory backup to preview.")
        raw = upload.stream.read(MAX_BYTES + 1)
        data = decode_backup(raw)
        identifier = instance_id(_database())
        with session_factory().begin() as db:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            baseline = fingerprint(db)
            with db.begin_nested() as savepoint:
                summary = restore_backup(db, data, identifier)
                savepoint.rollback()
        name = secrets.token_hex(24)
        path = _preview_directory() / (name + ".json")
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
        token = _signer().dumps({"file": name, "user": viewer.id, "session": _session_binding(),
                                 "baseline": baseline, "instance": identifier,
                                 "sha256": hashlib.sha256(raw).hexdigest()})
        return _page(preview=summary, token=token, created_at=data["created_at"])
    except RequestEntityTooLarge:
        return _page(error="Choose an inventory backup smaller than 5 MB.", status=413)
    except BackupError as exc:
        return _page(error=str(exc), status=400)
    except IntegrityError:
        return _page(error="The backup conflicts with existing inventory names or references. Nothing was changed.", status=409)
    except OperationalError:
        return _page(error="Inventory is busy. Try previewing the backup again shortly.", status=409)


@backup_bp.post("/restore")
@login_required
def restore() -> ResponseReturnValue:
    """Apply an unexpired, session-bound preview if curated state is unchanged.

    Returns:
        ResponseReturnValue: The Backup panel after restoring, or with the
            reason the restore was refused.
    """
    viewer = _admin()
    path = None
    try:
        if request.form.get("confirm") != "restore":
            raise BackupError("Confirm that you want to apply the reviewed backup.")
        token = _signer().loads(request.form.get("restore_token", ""), max_age=PREVIEW_SECONDS)
        if not isinstance(token, dict) or token.get("user") != viewer.id or token.get("session") != _session_binding():
            raise BackupError("This restore preview belongs to another sign-in. Preview the file again.")
        name = token.get("file", "")
        if not isinstance(name, str) or len(name) != 48 or any(c not in "0123456789abcdef" for c in name):
            raise BackupError("This restore preview is invalid. Preview the file again.")
        path = _preview_directory() / (name + ".json")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != token.get("sha256"):
            raise BackupError("The uploaded file changed. Preview the backup again.")
        data = decode_backup(raw)
        identifier = instance_id(_database())
        with session_factory().begin() as db:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            if identifier != token.get("instance") or fingerprint(db) != token.get("baseline"):
                raise BackupError("Inventory changed since this preview. Preview the backup again before restoring.")
            summary = restore_backup(db, data, identifier)
            _audit(db, viewer, "inventory.backup.restored", {
                "version": data["version"], "equipment": len(data["tables"]["devices"]),
                "cleared_placements": summary["cleared_placements"], "matched": summary["matched"],
            })
        if not summary["same_instance"]:
            adopt_instance(_database(), data["instance_id"])
        path.unlink(missing_ok=True)
        return redirect(url_for("private.settings", panel="backup", saved="restore"))
    except (BadSignature, FileNotFoundError):
        return _page(error="This restore preview expired or was already used. Preview the file again.", status=400)
    except BackupError as exc:
        return _page(error=str(exc), status=409)
    except IntegrityError:
        return _page(error="The backup conflicts with current inventory. Nothing was changed. Preview the file again.", status=409)
    except OperationalError:
        return _page(error="Inventory is busy. Nothing was changed; try again shortly.", status=409)
