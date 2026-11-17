# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""The release script: snapshots without history, merges, checks, and version bumps."""

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("publish", ROOT / "scripts" / "publish.py")
publish = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publish)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Tester", "GIT_AUTHOR_EMAIL": "tester@example.invalid",
    "GIT_COMMITTER_NAME": "Tester", "GIT_COMMITTER_EMAIL": "tester@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


def sh(repo, *args):
    """Run Git in a test repository and return its output.

    Args:
        repo: The repository folder.
        *args: Git's arguments.

    Returns:
        str: Standard output, stripped.
    """
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def write(root, relative, text):
    """Write a file inside a test tree, creating its folders.

    Args:
        root: The tree's folder.
        relative: The file's relative path.
        text: Its contents.

    Returns:
        None
    """
    path = Path(root) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class PublishTests(unittest.TestCase):
    """Run the script against throwaway private and public repositories."""

    def setUp(self):
        """Create a private repository with a release branch and an empty public one."""
        self.saved_env = {key: os.environ.get(key) for key in GIT_ENV}
        os.environ.update(GIT_ENV)
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.private, self.public = base / "private", base / "public"
        self.denylist = base / "denylist.txt"
        self.denylist.write_text("# private names\nsecret-lab\\.example\n")
        sh(base, "init", "--quiet", "-b", "main", str(self.private))
        sh(base, "init", "--quiet", "-b", "main", str(self.public))
        write(self.private, "pyproject.toml", '[project]\nname = "labrat"\nversion = "0.5.0"\n')
        write(self.private, "src/labrat/__init__.py", '"""Package."""\n\n__version__ = "0.5.0"\n')
        write(self.private, "README.md", "LabRat\n")
        write(self.private, "AGENTS.md", "private workflow\n")
        write(self.private, ".gitattributes", "AGENTS.md export-ignore\n")
        sh(self.private, "add", "-A")
        sh(self.private, "commit", "--quiet", "-m", "Private commit message")
        sh(self.private, "branch", "release")

    def tearDown(self):
        """Remove the repositories and restore the environment."""
        self.temp.cleanup()
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def run_script(self, *args):
        """Run the script quietly.

        Args:
            *args: Its command line.

        Returns:
            tuple[int, str]: Exit status and combined output.
        """
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            status = publish.main(list(args))
        return status, out.getvalue()

    def export(self, *extra):
        """Export the release branch into the public repository.

        Args:
            *extra: Additional options.

        Returns:
            tuple[int, str]: Exit status and output.
        """
        return self.run_script(
            "export", "--source", str(self.private), "--public", str(self.public), "--remote", "",
            "--denylist", str(self.denylist), "--skip-gate", *extra,
        )

    def test_first_release_has_no_private_history(self):
        status, output = self.export("--bump", "none")
        self.assertEqual(status, 0, output)
        log = sh(self.public, "log", "--format=%s", "--all")
        self.assertNotIn("Private commit message", log)
        self.assertEqual(sh(self.public, "tag"), "v0.5.0")
        self.assertTrue((self.public / "README.md").is_file())
        self.assertFalse((self.public / "AGENTS.md").exists())

    def test_first_release_overwrites_a_starter_commit(self):
        write(self.public, "README.md", "# labrat\n")
        write(self.public, "LICENSE", "starter license\n")
        sh(self.public, "add", "-A")
        sh(self.public, "commit", "--quiet", "-m", "first commit")
        status, output = self.export("--bump", "none")
        self.assertEqual(status, 0, output)
        self.assertEqual((self.public / "README.md").read_text(), "LabRat\n")
        self.assertIn("kept from the repository's starter commit: LICENSE", output)
        self.assertEqual(sh(self.public, "status", "--porcelain"), "")

    def test_second_release_keeps_public_changes(self):
        self.assertEqual(self.export("--bump", "none")[0], 0)
        write(self.public, "CONTRIBUTORS", "a public contributor\n")
        sh(self.public, "add", "-A")
        sh(self.public, "commit", "--quiet", "-m", "Public change")
        write(self.private, "README.md", "LabRat, now better\n")
        sh(self.private, "commit", "--quiet", "-am", "Another private message")
        sh(self.private, "branch", "-f", "release", "main")
        status, output = self.export("--version", "0.6.0", "--bump", "none")
        self.assertEqual(status, 0, output)
        self.assertTrue((self.public / "CONTRIBUTORS").is_file())
        self.assertEqual((self.public / "README.md").read_text(), "LabRat, now better\n")
        self.assertNotIn("Another private message", sh(self.public, "log", "--format=%s", "--all"))
        self.assertEqual(sh(self.public, "rev-list", "--count", "release-snapshots"), "2")

    def test_import_writes_public_commits_as_patches(self):
        self.export("--bump", "none")
        write(self.public, "CONTRIBUTORS", "a public contributor\n")
        sh(self.public, "add", "-A")
        sh(self.public, "commit", "--quiet", "-m", "Public change")
        out = Path(self.temp.name) / "patches"
        status, output = self.run_script("import", "--public", str(self.public), "--out", str(out))
        self.assertEqual(status, 0, output)
        self.assertEqual(len(list(out.glob("*.patch"))), 1)

    def test_denylisted_text_stops_the_release(self):
        write(self.private, "docs/notes.md", "see secret-lab.example\n")
        sh(self.private, "add", "-A")
        sh(self.private, "commit", "--quiet", "-m", "Leak")
        sh(self.private, "branch", "-f", "release", "main")
        status, output = self.export()
        self.assertEqual(status, 1)
        self.assertIn("docs/notes.md:1: denylist pattern 1", output)
        self.assertNotIn("secret-lab", output)
        self.assertEqual(sh(self.public, "branch"), "")

    def test_literal_hints_skip_files_fast_without_missing_matches(self):
        bounded = publish.re.compile(r"(?i)(?<![\w.-])Rack\-A(?![\w-])")
        self.assertEqual(publish.literal_hint(bounded), "rack-a")
        self.assertIsNone(publish.literal_hint(publish.re.compile(r"10\.0\.\d+\.\d+")))
        tree = Path(self.temp.name) / "tree"
        write(tree, "notes.md", "first\nsee RACK-A and 10.0.4.2\n")
        write(tree, "clean.md", "nothing here, not even rack-ab\n")
        problems = publish.check_snapshot(tree, [bounded, publish.re.compile(r"10\.0\.\d+\.\d+")])
        self.assertEqual(problems, ["notes.md:2: denylist pattern 1", "notes.md:2: denylist pattern 2"])

    def test_private_files_stop_the_release(self):
        write(self.private, "local_config.toml", "[auth]\n")
        sh(self.private, "add", "-f", "-A")
        sh(self.private, "commit", "--quiet", "-m", "Oops")
        sh(self.private, "branch", "-f", "release", "main")
        status, output = self.export()
        self.assertEqual(status, 1)
        self.assertIn("local_config.toml: private file", output)

    def test_missing_denylist_refuses(self):
        self.denylist.unlink()
        status, output = self.export()
        self.assertEqual(status, 1)
        self.assertIn("no denylist", output)

    def test_release_rolls_the_version_forward(self):
        status, output = self.export()
        self.assertEqual(status, 0, output)
        self.assertIn("version/0.5.1", output)
        self.assertEqual(sh(self.private, "show", "version/0.5.1:pyproject.toml").splitlines()[-1], 'version = "0.5.1"')
        self.assertIn('__version__ = "0.5.1"', sh(self.private, "show", "version/0.5.1:src/labrat/__init__.py"))
        self.assertEqual(sh(self.private, "rev-parse", "v0.5.0^{commit}"), sh(self.private, "rev-parse", "release"))
        self.assertEqual(sh(self.private, "branch", "--show-current"), "main")
        self.assertEqual(sh(self.private, "status", "--porcelain"), "")

    def test_bump_alone_and_its_parts(self):
        status, output = self.run_script("bump", "--source", str(self.private), "--remote", "", "--part", "patch")
        self.assertEqual(status, 0, output)
        self.assertIn("version/0.5.1", sh(self.private, "branch"))
        self.assertEqual(publish.next_version("1.4.2", "major"), "2.0.0")
        self.assertEqual(publish.next_version("1.4.2", "minor"), "1.5.0")
        with self.assertRaises(publish.PublishError):
            publish.next_version("1.4", "minor")

    def test_disagreeing_version_files_refuse(self):
        write(self.private, "src/labrat/__init__.py", '__version__ = "0.4.0"\n')
        sh(self.private, "commit", "--quiet", "-am", "Drift")
        sh(self.private, "branch", "-f", "release", "main")
        status, output = self.export()
        self.assertEqual(status, 1)
        self.assertIn("version files disagree", output)

    def test_unchanged_release_and_existing_tag_refuse(self):
        self.export("--bump", "none")
        status, output = self.export("--version", "0.5.0", "--bump", "none")
        self.assertIn("already has tag v0.5.0", output)
        status, output = self.export("--version", "0.5.9", "--bump", "none")
        self.assertEqual(status, 1)
        self.assertIn("nothing to publish", output)
        self.assertEqual(sh(self.public, "branch", "--show-current"), "main")

    def test_changelog_needs_an_entry_for_the_version(self):
        write(self.private, "CHANGELOG.md", "# Changelog\n\n## 0.4.9\n\n- Older.\n")
        sh(self.private, "add", "-A")
        sh(self.private, "commit", "--quiet", "-m", "Changelog")
        sh(self.private, "branch", "-f", "release", "main")
        status, output = self.export("--bump", "none")
        self.assertEqual(status, 1)
        self.assertIn("no '## 0.5.0' entry", output)
        write(self.private, "CHANGELOG.md", "# Changelog\n\n## 0.5.0\n\n- New.\n")
        sh(self.private, "commit", "--quiet", "-am", "Entry")
        sh(self.private, "branch", "-f", "release", "main")
        self.assertEqual(self.export("--bump", "none")[0], 0)

    def test_dry_run_leaves_public_alone(self):
        status, output = self.export("--dry-run")
        self.assertEqual(status, 0, output)
        self.assertEqual(sh(self.public, "branch"), "")
        self.assertNotIn("version/", sh(self.private, "branch"))


if __name__ == "__main__":
    unittest.main()
