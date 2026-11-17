# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Exercise the split Flask route tree, static assets, and API responses."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from alembic import command
from alembic.config import Config


class AppSmokeTests(unittest.TestCase):
    """Run smoke tests against a migrated, isolated temporary SQLite file."""

    def setUp(self) -> None:
        """Provide credentials, migrate a test database, and open a client."""
        temporary = TemporaryDirectory(prefix="labrat-app-test-")
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        configuration = directory / "config.toml"
        configuration.write_text(
            "[database]\n"
            f"path = {json.dumps(str(directory / 'labrat.sqlite3'))}\n"
            "[auth]\n"
            f"secret_key = {json.dumps('test-' + 'a' * 64)}\n",
            encoding="utf-8",
        )
        environment = patch.dict(os.environ, {"LABRAT_CONFIG": str(configuration)})
        environment.start()
        self.addCleanup(environment.stop)
        command.upgrade(
            Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")),
            "head",
        )
        # Import inside setUp because labrat.app also exposes a Gunicorn app.
        from labrat.app import create_app

        self.app = create_app()
        self.addCleanup(self.app.extensions["labrat_engine"].dispose)
        self.client = self.app.test_client()

    def test_hello(self) -> None:
        """Confirm root returns an HTML landing page offering initial setup."""
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content_type.startswith("text/html"))
        self.assertIn(b"First-time setup", response.data)

    def test_health(self) -> None:
        """Keep the original public API response unchanged."""
        response = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"service": "labrat", "status": "ok"})

    def test_static_assets(self) -> None:
        """Ensure Flask serves readable CSS and the frontend JavaScript."""
        for path in ("/static/css/main.css", "/static/js/main.js"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                response.close()

    def test_private_routes_require_signin(self) -> None:
        """Redirect anonymous HTML and return JSON 401 on protected APIs."""
        for path in ("/home", "/profile"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.headers["Location"].endswith("/signin"))
        response = self.client.get("/api/v1/me")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json, {"error": "authentication_required"})


if __name__ == "__main__":
    unittest.main()
