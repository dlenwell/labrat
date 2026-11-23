# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Verify LabRat's one-time setup, Argon2id credentials, and browser sessions."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from argon2 import PasswordHasher, Type
from sqlalchemy import select

from labrat.models import AuthIdentity, LocalCredential, User


class AuthenticationTests(unittest.TestCase):
    """Test authentication using a new temporary database for each test."""

    def setUp(self) -> None:
        """Create a migrated database, valid local config, and test client."""
        temporary = TemporaryDirectory(prefix="labrat-auth-test-")
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        self.settings = directory / "local_config.toml"
        self.settings.write_text(
            "[database]\n"
            f"path = {json.dumps(str(directory / 'labrat.sqlite3'))}\n"
            "[auth]\n"
            f"secret_key = {json.dumps('test-' + 'b' * 64)}\n",
            encoding="utf-8",
        )
        environment = patch.dict(os.environ, {"LABRAT_CONFIG": str(self.settings)})
        environment.start()
        self.addCleanup(environment.stop)
        command.upgrade(
            Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")),
            "head",
        )
        from labrat.app import create_app

        self.app = create_app()
        self.addCleanup(self.app.extensions["labrat_engine"].dispose)
        self.client = self.app.test_client()

    def csrf(self) -> str:
        """Get the CSRF token from the current test-client session.

        Returns:
            str: A generated form token.
        """
        with self.client.session_transaction() as data:
            return data["csrf_token"]

    def setup_code(self) -> str:
        """Read the first-run code the way an operator would.

        Returns:
            str: The code from the file beside the database.
        """
        return self.app.extensions["labrat_setup_code_path"].read_text().strip()

    def create_admin(self) -> None:
        """Submit the first-run form with a strong sample password."""
        self.assertEqual(self.client.get("/setup").status_code, 200)
        response = self.client.post(
            "/setup",
            data={
                "_csrf_token": self.csrf(),
                "setup_code": self.setup_code(),
                "username": "ExampleAdmin",
                "password": "correct-horse-battery-staple-2026",
                "confirm_password": "correct-horse-battery-staple-2026",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/home"))

    def test_setup_accepts_a_configured_remote_host(self) -> None:
        """Allow first-run setup through a host listed in local config."""
        self.settings.write_text(
            "[server]\n"
            'allowed_hosts = ["localhost", "127.0.0.1", "192.0.2.25"]\n'
            "[database]\n"
            f"path = {json.dumps(str(self.settings.parent / 'labrat.sqlite3'))}\n"
            "[auth]\n"
            f"secret_key = {json.dumps('test-' + 'b' * 64)}\n",
            encoding="utf-8",
        )
        from labrat.app import create_app

        app = create_app()
        self.addCleanup(app.extensions["labrat_engine"].dispose)
        client = app.test_client()
        response = client.get(
            "/setup",
            environ_overrides={
                "REMOTE_ADDR": "192.0.2.25",
                "HTTP_HOST": "192.0.2.25",
            },
        )
        self.assertEqual(response.status_code, 200)

    def test_setup_requires_the_one_time_code(self) -> None:
        """Setup refuses a wrong code, accepts the printed one, then removes it."""
        path = self.app.extensions["labrat_setup_code_path"]
        self.assertTrue(path.is_file())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.client.get("/setup")
        form = {"_csrf_token": self.csrf(), "username": "admin",
                "password": "correct-horse-battery-staple-2026",
                "confirm_password": "correct-horse-battery-staple-2026"}
        refused = self.client.post("/setup", data={**form, "setup_code": "0000-0000-0000"})
        self.assertEqual(refused.status_code, 200)
        self.assertIn(b"setup code is not right", refused.data)
        code = self.setup_code()
        accepted = self.client.post("/setup", data={**form, "setup_code": " " + code.upper() + " "})
        self.assertEqual(accepted.status_code, 302)
        self.assertFalse(path.exists())

    def test_setup_requires_csrf(self) -> None:
        """Reject a creation attempt without a signed session form token."""
        self.client.get("/setup")
        response = self.client.post(
            "/setup",
            data={
                "username": "admin",
                "password": "correct-horse-battery-staple-2026",
                "confirm_password": "correct-horse-battery-staple-2026",
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_setup_validates_passwords(self) -> None:
        """Keep setup available when the submitted password is too short."""
        self.client.get("/setup")
        response = self.client.post(
            "/setup",
            data={
                "_csrf_token": self.csrf(),
                "setup_code": self.setup_code(),
                "username": "admin",
                "password": "short",
                "confirm_password": "short",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Choose a password", response.data)
        self.assertEqual(self.client.get("/setup").status_code, 200)

    def test_admin_creates_argon2id_hash_and_closes_setup(self) -> None:
        """Persist the local identity without any stored plaintext password."""
        self.create_admin()
        factory = self.app.extensions["labrat_session_factory"]
        with factory() as db:
            user = db.scalar(select(User))
            identity = db.scalar(select(AuthIdentity))
            credentials = db.get(LocalCredential, user.id)
            self.assertTrue(user.is_admin)
            self.assertEqual(user.username, "exampleadmin")
            self.assertEqual(identity.provider, "local")
            self.assertTrue(credentials.password_hash.startswith("$argon2id$"))
            self.assertTrue(
                PasswordHasher(type=Type.ID).verify(
                    credentials.password_hash, "correct-horse-battery-staple-2026"
                )
            )
        self.assertEqual(self.client.get("/setup").status_code, 404)
        self.assertEqual(self.client.get("/signup").status_code, 404)

    def test_authenticated_pages_and_api(self) -> None:
        """Render private UI and return only safe profile fields as JSON."""
        self.create_admin()
        self.assertEqual(self.client.get("/home").status_code, 200)
        self.assertIn(b"exampleadmin", self.client.get("/profile").data)
        response = self.client.get("/api/v1/me")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["username"], "exampleadmin")
        self.assertTrue(response.json["is_admin"])
        self.assertNotIn("password_hash", response.json)

    def test_signout_then_signin(self) -> None:
        """Expire browser access and allow a new local password login."""
        self.create_admin()
        response = self.client.post("/signout", data={"_csrf_token": self.csrf()})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/api/v1/me").status_code, 401)
        self.assertEqual(self.client.get("/signin").status_code, 200)
        response = self.client.post(
            "/signin",
            data={
                "_csrf_token": self.csrf(),
                "username": "EXAMPLEADMIN",
                "password": "correct-horse-battery-staple-2026",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/home").status_code, 200)

    def test_bad_password_is_rejected(self) -> None:
        """Return a generic failure message without authenticating the user."""
        self.create_admin()
        self.client.post("/signout", data={"_csrf_token": self.csrf()})
        self.client.get("/signin")
        response = self.client.post(
            "/signin",
            data={
                "_csrf_token": self.csrf(),
                "username": "exampleadmin",
                "password": "wrong-password",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Incorrect username or password", response.data)
        self.assertEqual(self.client.get("/api/v1/me").status_code, 401)

    def test_repeated_failures_lock_sign_in_and_are_audited(self) -> None:
        """Ten failures refuse even the right password for a while."""
        from labrat.audit import AuditEvent
        self.create_admin()
        self.client.post("/signout", data={"_csrf_token": self.csrf()})
        self.client.get("/signin")
        for _ in range(10):
            self.client.post("/signin", data={
                "_csrf_token": self.csrf(), "username": "exampleadmin", "password": "wrong-password"})
        response = self.client.post("/signin", data={
            "_csrf_token": self.csrf(), "username": "exampleadmin",
            "password": "correct-horse-battery-staple-2026"})
        self.assertEqual(response.status_code, 429)
        self.assertIn(b"Too many failed sign-ins", response.data)
        self.assertEqual(self.client.get("/api/v1/me").status_code, 401)
        with self.app.extensions["labrat_session_factory"]() as db:
            actions = db.scalars(select(AuditEvent.action)).all()
        self.assertEqual(actions.count("auth.signin.throttled"), 1)

    def test_signing_out_ends_copies_of_the_session(self) -> None:
        """A copied session cookie stops working once its owner signs out."""
        self.create_admin()
        copy = self.app.test_client()
        copy.set_cookie("session", self.client.get_cookie("session").value)
        self.assertEqual(copy.get("/api/v1/me").status_code, 200)
        self.client.post("/signout", data={"_csrf_token": self.csrf()})
        self.assertEqual(copy.get("/api/v1/me").status_code, 401)
        self.client.get("/signin")
        self.client.post("/signin", data={
            "_csrf_token": self.csrf(), "username": "exampleadmin",
            "password": "correct-horse-battery-staple-2026"})
        self.assertEqual(self.client.get("/api/v1/me").status_code, 200)

    def test_unknown_account_does_the_same_argon2_work(self) -> None:
        """An unknown username still runs one Argon2 verification."""
        self.create_admin()
        self.client.post("/signout", data={"_csrf_token": self.csrf()})
        self.client.get("/signin")
        with patch.object(PasswordHasher, "verify", autospec=True, side_effect=PasswordHasher.verify) as verify:
            response = self.client.post(
                "/signin",
                data={"_csrf_token": self.csrf(), "username": "nobody-here", "password": "wrong-password"},
            )
        self.assertIn(b"Incorrect username or password", response.data)
        self.assertEqual(verify.call_count, 1)

    def test_disabled_account_loses_access(self) -> None:
        """Reject a previously signed-in user after deactivation."""
        self.create_admin()
        factory = self.app.extensions["labrat_session_factory"]
        with factory.begin() as db:
            db.scalar(select(User)).is_active = False
        self.assertEqual(self.client.get("/api/v1/me").status_code, 401)
        self.assertEqual(self.client.get("/home").status_code, 302)

    def test_oversized_requests_are_refused_before_reading(self) -> None:
        """A public form larger than the app-wide limit gets HTTP 413."""
        self.client.get("/setup")
        response = self.client.post(
            "/setup", data={"_csrf_token": self.csrf(), "username": "x" * (300 * 1024)},
        )
        self.assertEqual(response.status_code, 413)

    def test_remote_hosts_without_secure_cookies_warn_at_start(self) -> None:
        """Allowing other machines over plain cookies prints a reminder."""
        import contextlib
        import io
        from labrat.app import create_app
        self.settings.write_text(self.settings.read_text() + '[server]\nallowed_hosts = ["localhost", "labrat.example.lan"]\n')
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            app = create_app()
        app.extensions["labrat_engine"].dispose()
        self.assertIn("secure_cookies is off", output.getvalue())
        self.assertIn("labrat.example.lan", output.getvalue())
        self.settings.write_text(self.settings.read_text().replace(
            "[auth]\n", "[auth]\nsecure_cookies = true\n"))
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            app = create_app()
        app.extensions["labrat_engine"].dispose()
        self.assertNotIn("secure_cookies is off", output.getvalue())

    def test_cookie_is_http_only(self) -> None:
        """Apply HTTP-only and SameSite-Lax protections to the signed cookie."""
        response = self.client.get("/setup")
        cookies = response.headers.getlist("Set-Cookie")
        self.assertTrue(any("HttpOnly" in cookie for cookie in cookies))
        self.assertTrue(any("SameSite=Lax" in cookie for cookie in cookies))

    def test_missing_secret_fails_startup(self) -> None:
        """Refuse to create an application without a configured signing key."""
        from labrat.app import create_app

        self.settings.write_text("[database]\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "secret_key"):
            create_app()


    OLD_PASSWORD = "correct-horse-battery-staple-2026"
    NEW_PASSWORD = "a-brand-new-example-passphrase"

    def change_password(self, current, new, confirm=None, client=None):
        """Submit the profile page's change-password form.

        Args:
            current: The current password typed.
            new: The new password.
            confirm: The repeat; defaults to ``new``.
            client: Test client to submit from; defaults to the main one.

        Returns:
            object: The response.
        """
        client = client or self.client
        with client.session_transaction() as data:
            token = data["csrf_token"]
        return client.post("/profile/password", data={
            "_csrf_token": token, "current_password": current,
            "new_password": new, "confirm_password": new if confirm is None else confirm,
        })

    def stored_hash(self):
        """Return the admin's stored password hash.

        Returns:
            str: The Argon2id hash.
        """
        with self.app.extensions["labrat_session_factory"]() as db:
            return db.scalar(select(LocalCredential.password_hash))

    def test_change_password_keeps_this_session_and_ends_others(self) -> None:
        """A change signs other browsers out, keeps this one, and is audited."""
        self.create_admin()
        other = self.app.test_client()
        other.get("/signin")
        with other.session_transaction() as data:
            token = data["csrf_token"]
        other.post("/signin", data={"_csrf_token": token, "username": "exampleadmin",
                                    "password": self.OLD_PASSWORD})
        self.assertEqual(other.get("/home").status_code, 200)
        self.assertIn(b"Change password", self.client.get("/profile").data)

        response = self.change_password(self.OLD_PASSWORD, self.NEW_PASSWORD)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/profile?password=changed", response.headers["Location"])
        self.assertIn(b"Password changed.", self.client.get(response.headers["Location"]).data)
        self.assertEqual(self.client.get("/home").status_code, 200)
        self.assertEqual(other.get("/home").status_code, 302)
        self.assertTrue(PasswordHasher(type=Type.ID).verify(self.stored_hash(), self.NEW_PASSWORD))
        self.assertIn(b"Changed your password", self.client.get("/profile").data)

        self.client.post("/signout", data={"_csrf_token": self.csrf()})
        self.client.get("/signin")
        for password, status in ((self.OLD_PASSWORD, 200), (self.NEW_PASSWORD, 302)):
            response = self.client.post("/signin", data={
                "_csrf_token": self.csrf(), "username": "exampleadmin", "password": password})
            self.assertEqual(response.status_code, status)

    def test_change_password_refusals_change_nothing(self) -> None:
        """Wrong current password, weak or mismatched new ones, and bad tokens are refused."""
        self.create_admin()
        before = self.stored_hash()
        cases = (
            (("not-the-current-password", self.NEW_PASSWORD), b"current password is not right"),
            ((self.OLD_PASSWORD, "too-short"), b"between 12 and 1024"),
            ((self.OLD_PASSWORD, self.NEW_PASSWORD, "something-else-entirely"), b"do not match"),
            ((self.OLD_PASSWORD, self.OLD_PASSWORD), b"different from your current one"),
        )
        for arguments, message in cases:
            with self.subTest(message=message):
                response = self.change_password(*arguments)
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.data)
        response = self.client.post("/profile/password", data={
            "_csrf_token": "wrong", "current_password": self.OLD_PASSWORD,
            "new_password": self.NEW_PASSWORD, "confirm_password": self.NEW_PASSWORD})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.stored_hash(), before)
        self.assertEqual(self.client.get("/home").status_code, 200, "refusals keep the session")

    def test_wrong_current_passwords_are_throttled(self) -> None:
        """Guessing the current password from a signed-in browser hits the sign-in limit."""
        self.create_admin()
        limit = self.app.extensions["labrat_signin_throttle"].limit
        for _attempt in range(limit):
            self.assertEqual(self.change_password("a-wrong-guess-here", self.NEW_PASSWORD).status_code, 400)
        response = self.change_password(self.OLD_PASSWORD, self.NEW_PASSWORD)
        self.assertEqual(response.status_code, 429)
        self.assertTrue(PasswordHasher(type=Type.ID).verify(self.stored_hash(), self.OLD_PASSWORD))

    def test_accounts_without_a_local_password_get_no_form(self) -> None:
        """The form is hidden, and the route refuses, for accounts with no local password."""
        self.create_admin()
        with self.app.extensions["labrat_session_factory"].begin() as db:
            db.delete(db.scalar(select(LocalCredential)))
        self.assertNotIn(b"Change password", self.client.get("/profile").data)
        self.assertEqual(self.change_password(self.OLD_PASSWORD, self.NEW_PASSWORD).status_code, 404)

if __name__ == "__main__":
    unittest.main()
