# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Export one whole install and import it over another, replacing everything."""

import io
import json
import sqlite3
import tomllib
import unittest
import zipfile

from sqlalchemy import select

import test_step4c as fixture
from labrat.database import database_path
from labrat.discovery.models import DiscoverySource
from labrat.instance_transfer import TransferError, export_instance, read_instance
from labrat.models import Device, User

OLD_SETTINGS = '\n[unifi]\nurl = "https://controller.example"\napi_key = "test-old-key"\n'
NEW_SETTINGS = '\n[proxmox]\nurl = "https://compute.example"\ntoken_id = "user@pve!test"\ntoken_secret = "test-new-secret"\n'


class InstanceTransferTests(unittest.TestCase):
    """Run the export and import between two temporary installs."""

    def install(self, settings):
        """Start a migrated install with an admin and extra configuration.

        Args:
            settings: TOML appended to the install's configuration.

        Returns:
            Step4CTests: The running install (its app, client, and factory).
        """
        site = fixture.Step4CTests()
        site.setUp()
        self.addCleanup(site.doCleanups)
        path = site.app.extensions["labrat_config_path"]
        path.write_text(path.read_text() + settings)
        return site

    @staticmethod
    def paths(site):
        """Return an install's database and configuration paths.

        Args:
            site: The install.

        Returns:
            tuple: Database path and configuration path.
        """
        return database_path(site.app.extensions["labrat_local_config"]), site.app.extensions["labrat_config_path"]

    def export(self, site):
        """Export an install.

        Args:
            site: The install.

        Returns:
            bytes: The export file.
        """
        with site.factory.begin() as db:
            db.add(Device(name="Old equipment"))
            db.add(DiscoverySource(name="unifi:default", kind="unifi", config_name="default"))
        return export_instance(*self.paths(site), "test")[0]

    def upload(self, site, data, confirm="replace"):
        """Post an export to an install's import route.

        Args:
            site: The install receiving it.
            data: The file.
            confirm: The confirmation checkbox's value.

        Returns:
            object: The response.
        """
        return site.client.post("/settings/backup/import", data={
            "_csrf_token": site.csrf, "confirm": confirm,
            "export": (io.BytesIO(data), "labrat-export.zip"),
        }, content_type="multipart/form-data")

    def test_export_holds_database_and_lab_settings_only(self):
        """The ZIP has the snapshot and connections, but no host sections."""
        old = self.install(OLD_SETTINGS)
        response = old.client.post("/settings/backup/export", data={"_csrf_token": old.csrf})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            self.assertEqual(set(archive.namelist()), {"manifest.json", "labrat.sqlite3", "local_config.toml"})
            settings = tomllib.loads(archive.read("local_config.toml").decode())
            manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual(settings, {"unifi": {"url": "https://controller.example", "api_key": "test-old-key"}})
        self.assertEqual(manifest["format"], "labrat-instance")
        self.assertEqual(read_instance(response.data).manifest["schema"], manifest["schema"])

    def test_import_replaces_everything_but_the_host_settings(self):
        """Inventory, accounts, and connections come from the export; host sections stay."""
        old = self.install(OLD_SETTINGS)
        data = self.export(old)
        new = self.install(NEW_SETTINGS)
        database, config_path = self.paths(new)
        host = tomllib.loads(config_path.read_text())
        with new.factory.begin() as db:
            db.add(Device(name="New equipment"))
            db.scalar(select(User)).username = "someone-else"
        evidence = database.with_name(database.name + ".access-evidence")
        evidence.mkdir()
        response = self.upload(new, data)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/signin?imported=1", response.headers["Location"])
        with new.factory() as db:
            self.assertEqual(list(db.scalars(select(Device.name))), ["Old equipment"])
            self.assertEqual(list(db.scalars(select(User.username))), ["admin"])
            self.assertEqual(db.scalar(select(DiscoverySource.name)), "unifi:default")
        settings = tomllib.loads(config_path.read_text())
        self.assertEqual(settings["unifi"]["api_key"], "test-old-key")
        self.assertNotIn("proxmox", settings)
        self.assertEqual((settings["database"], settings["auth"]), (host["database"], host["auth"]))
        self.assertFalse(evidence.exists())
        self.assertEqual(new.client.get("/home").status_code, 302, "the importer is signed out")

    def test_older_export_is_migrated(self):
        """An export from an older schema is upgraded to this one on import."""
        old = self.install(OLD_SETTINGS)
        data = self.export(old)
        new = self.install("")
        database, _config = self.paths(new)
        head = sqlite3.connect(database).execute("SELECT version_num FROM alembic_version").fetchone()[0]
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        snapshot = database.with_name("older.sqlite3")
        snapshot.write_bytes(files["labrat.sqlite3"])
        connection = sqlite3.connect(snapshot)
        with connection:
            connection.execute("UPDATE alembic_version SET version_num = '0015_user_session_version'")
        connection.close()
        files["labrat.sqlite3"] = snapshot.read_bytes()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        self.assertEqual(self.upload(new, buffer.getvalue()).status_code, 302)
        version = sqlite3.connect(database).execute("SELECT version_num FROM alembic_version").fetchone()[0]
        self.assertEqual(version, head)

    def test_refusals_change_nothing(self):
        """Missing confirmation, other files, newer schemas, and non-admins are refused."""
        old = self.install(OLD_SETTINGS)
        data = self.export(old)
        new = self.install(NEW_SETTINGS)
        _database, config_path = self.paths(new)
        before = config_path.read_bytes()
        self.assertEqual(self.upload(new, data, confirm="").status_code, 400)
        self.assertIn(b"This is not a LabRat export", self.upload(new, b"not a zip").data)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        snapshot = config_path.with_name("newer.sqlite3")
        snapshot.write_bytes(files["labrat.sqlite3"])
        connection = sqlite3.connect(snapshot)
        with connection:
            connection.execute("UPDATE alembic_version SET version_num = '9999_from_the_future'")
        connection.close()
        files["labrat.sqlite3"] = snapshot.read_bytes()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, content in files.items():
                archive.writestr(name, content)
        self.assertIn(b"newer LabRat", self.upload(new, buffer.getvalue()).data)
        with self.assertRaises(TransferError):
            read_instance(buffer.getvalue())
        self.assertEqual(config_path.read_bytes(), before)
        with new.factory() as db:
            self.assertEqual(list(db.scalars(select(Device.name))), [])
        with new.factory.begin() as db:
            db.scalar(select(User)).is_admin = False
        self.assertEqual(self.upload(new, data).status_code, 403)
        self.assertEqual(new.client.post("/settings/backup/export", data={"_csrf_token": new.csrf}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
