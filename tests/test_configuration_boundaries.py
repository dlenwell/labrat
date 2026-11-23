# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Validate local configuration boundaries and public documentation access."""

import importlib
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import test_app as fixture
from labrat.config import load_local_config
from labrat.database import database_path


class ConfigurationTests(unittest.TestCase):
    """Use only temporary config paths, never the checkout's private settings."""

    def test_missing_default_and_explicit_paths_have_different_semantics(self):
        """An explicit missing file is an error; an absent default is permitted."""
        with TemporaryDirectory() as directory, patch.dict('os.environ', {}, clear=True), patch.object(Path,'cwd',return_value=Path(directory)):
            self.assertEqual(load_local_config(),{})
            with patch.dict('os.environ',{'LABRAT_CONFIG':str(Path(directory)/'missing.toml')}), self.assertRaises(FileNotFoundError):
                load_local_config()
            path=Path(directory)/'settings.toml'
            path.write_text('[collector]\ninterval_seconds = 45\n')
            with patch.dict('os.environ',{'LABRAT_CONFIG':str(path)}):
                self.assertEqual(load_local_config(),{'collector':{'interval_seconds':45}})

    def test_invalid_database_settings_fail_before_engine_creation(self):
        """Reject malformed tables and empty filenames."""
        for settings, error in (([],TypeError), ({'path':''},ValueError), ({'path':True},ValueError)):
            with self.subTest(settings=settings), self.assertRaises(error):
                database_path({'database':settings})


class SecretFileIgnoreTests(unittest.TestCase):
    """Credential and agent environment files can never be committed."""

    SECRET_NAMES = ("creds.txt", "AGENT-ENV-CREDS.env", "local_config.toml")

    def setUp(self):
        """Run against this checkout's Git, skipping outside a repository."""
        self.root = Path(__file__).resolve().parents[1]
        if shutil.which("git") is None or not (self.root / ".git").exists():
            self.skipTest("not a Git checkout")

    def git(self, *args):
        """Run a Git command in the checkout and return the completed process.

        Args:
            *args: Git's arguments.

        Returns:
            subprocess.CompletedProcess: The finished process.
        """
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True)

    def test_secret_files_are_ignored_anywhere(self):
        """Each name is ignored at the root and in any subfolder."""
        for name in self.SECRET_NAMES:
            for path in (name, f"src/labrat/{name}"):
                with self.subTest(path=path):
                    self.assertEqual(self.git("check-ignore", "-q", "--no-index", path).returncode, 0)

    def test_no_secret_file_is_tracked(self):
        """No tracked file has one of these names."""
        tracked = {Path(line).name for line in self.git("ls-files").stdout.splitlines()}
        self.assertFalse(tracked & set(self.SECRET_NAMES))


class AppConfigurationTests(unittest.TestCase):
    """Exercise startup policy and public routes against migrated test storage."""

    setUp = fixture.AppSmokeTests.setUp

    def test_startup_rejects_invalid_host_and_session_options(self):
        """Host allowlists and session options must have explicit, valid shapes."""
        module=importlib.import_module('labrat.app')
        valid=self.app.extensions['labrat_local_config']
        configs=[{**valid,'server':[]}, {**valid,'auth':[]},
                 {**valid,'auth':{'secret_key':'short'}}, {**valid,'auth':{'secret_key':'REPLACE_'+'x'*64}},
                 {**valid,'auth':{**valid['auth'],'secure_cookies':'false'}}]
        configs += [{**valid,'server':{'allowed_hosts':hosts}} for hosts in ([], 'localhost', [''], [' bad'], ['a/b'], [1])]
        for config in configs:
            with self.subTest(config_type=type(config.get('server'))), patch.object(module,'load_local_config',return_value=config), self.assertRaises((ValueError,RuntimeError)):
                module.create_app()

    def test_documentation_is_public_and_documents_route_access(self):
        """Public docs expose API contracts without requiring a local account."""
        for endpoint in ('/docs','/docs/setup','/docs/tour','/docs/inventory','/docs/discovery','/docs/api'):
            response=self.client.get(endpoint)
            self.assertEqual(response.status_code,200)
            self.assertNotIn(self.app.secret_key.encode(),response.data)
        response=self.client.get('/docs/api')
        self.assertIn(b'Admin + CSRF',response.data)
        self.assertIn(b'Signed-in',response.data)
        self.assertIn(b'API token (observations:mndp)',response.data)
        self.assertIn(b'/api/v1/health',response.data)
        self.assertIn(b'Public',response.data)
