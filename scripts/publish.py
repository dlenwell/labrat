#!/usr/bin/env python3
# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Publish a LabRat release to a public repository without the private history.

The private repository keeps its full history. The public repository gets
one commit per release: a snapshot of the private release branch, merged
into the public ``main`` so changes contributors made in public are kept.

Public repository layout:

- ``release-snapshots``: one commit per release holding exactly the
  exported tree; each one's parent is the previous release's snapshot.
  It is the merge base that lets a release keep public changes.
- ``main``: what people clone. Each release merges the new snapshot into
  it; a conflict stops the script for the owner to resolve. On the first
  release, a starter commit the host made (a placeholder README) is
  overwritten where the release has the same file.
- ``v<version>``: an annotated tag on that release's merge.

The snapshot is ``git archive`` of the release branch, so files marked
``export-ignore`` in ``.gitattributes`` are left out. Before anything is
written to the public repository, the snapshot must pass three checks: no
private files (local configuration, credentials, ``data/``, databases), no
line matching a pattern in the ignored denylist
(``data/publish-denylist.txt``, one regular expression per line), and the
validation gate run on the snapshot itself.

After a release is committed, the version rolls forward: a new private
branch ``version/<next>``, cut from the released commit, bumps every
version string (``pyproject.toml`` and ``labrat.__version__``) by the
``--bump`` part (patch by default: 0.5.1, then 0.5.2). It is built in a temporary worktree,
so the current checkout is untouched; push it and merge it like any other
branch. ``bump`` does that step on its own.

The released private commit is tagged ``v<version>`` too (locally; push
it with the command printed), which is where ``scripts/changelog.py``
starts the next release's notes. When the snapshot has a
``CHANGELOG.md``, it must have a ``## <version>`` entry.

Nothing is pushed unless ``--push`` is given (and it never pushes the
private bump branch). Usage::

    # Preview: build and check the snapshot, change nothing.
    .venv/bin/python scripts/publish.py export --public ../labrat-public --dry-run
    # Commit and merge the release locally, then review it.
    .venv/bin/python scripts/publish.py export --public ../labrat-public
    # Contributions made in public since the last release, as patches.
    .venv/bin/python scripts/publish.py import --public ../labrat-public --out /tmp/public-patches
    # Only roll the version forward (after finishing a conflicted release).
    .venv/bin/python scripts/publish.py bump --part patch
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_BRANCH = "release-snapshots"
PUBLIC_BRANCH = "main"
DEFAULT_DENYLIST = ROOT / "data" / "publish-denylist.txt"
#: Every file that carries the version, with the pattern whose group is it.
VERSION_FILES = {
    "pyproject.toml": re.compile(r'^version = "([^"]+)"$', re.M),
    "src/labrat/__init__.py": re.compile(r'^__version__ = "([^"]+)"$', re.M),
}
BUMP_PARTS = ("major", "minor", "patch")
#: Paths that must never reach the public repository, as fnmatch patterns
#: against the snapshot's relative paths (``.gitignore`` already keeps them
#: out of Git; this catches one tracked by mistake).
FORBIDDEN = (
    "local_config.toml", "*/local_config.toml", "AGENT-ENV-CREDS*", "*/AGENT-ENV-CREDS*",
    "creds.txt", "*.env", "data/*", "deploy/data/*", "deploy/config/*", "*.db", "*.sqlite*",
)


class PublishError(Exception):
    """A check failed or Git refused; the message says what to do."""


def git(repo, *args, data=None):
    """Run a Git command in a repository and return its output.

    Args:
        repo: The repository's working directory.
        *args: Git's arguments.
        data: Optional bytes for standard input.

    Returns:
        bytes: The command's standard output.

    Raises:
        PublishError: When Git exits non-zero.
    """
    result = subprocess.run(["git", "-C", str(repo), *args], input=data, capture_output=True)
    if result.returncode:
        message = result.stderr.decode(errors="replace").strip() or result.stdout.decode(errors="replace").strip()
        raise PublishError(f"git {' '.join(args)}: {message}")
    return result.stdout


def git_text(repo, *args):
    """Run a Git command and return its output as stripped text.

    Args:
        repo: The repository's working directory.
        *args: Git's arguments.

    Returns:
        str: Standard output without surrounding whitespace.
    """
    return git(repo, *args).decode().strip()


def has_ref(repo, ref):
    """Say whether a ref (branch, tag, or commit) exists.

    Args:
        repo: The repository's working directory.
        ref: The ref to look up.

    Returns:
        bool: True when it resolves to a commit.
    """
    try:
        git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    except PublishError:
        return False
    return True


def export_tree(source, ref, dest):
    """Extract a ref's tree, without ``export-ignore`` paths, into a folder.

    Args:
        source: The private repository.
        ref: The commit or branch to export.
        dest: An empty folder to fill.

    Returns:
        None
    """
    archive = git(source, "archive", "--format=tar", ref)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(dest, filter="data")


def snapshot_files(root):
    """List a snapshot's files as relative POSIX paths.

    Args:
        root: The snapshot folder.

    Returns:
        list[str]: Every file and symlink, sorted.
    """
    found = []
    for folder, dirs, files in os.walk(root):
        for name in files + [d for d in dirs if (Path(folder) / d).is_symlink()]:
            found.append((Path(folder) / name).relative_to(root).as_posix())
    return sorted(found)


def load_denylist(path):
    """Read the denylist's regular expressions, skipping blanks and comments.

    Args:
        path: The denylist file.

    Returns:
        list[re.Pattern]: The compiled patterns.

    Raises:
        PublishError: When the file is missing, empty, or has a bad pattern.
    """
    if not Path(path).is_file():
        raise PublishError(
            f"no denylist at {path}: create it (it is ignored by Git) with one regular expression "
            "per line for text that must never be published, such as private hostnames"
        )
    patterns = []
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            patterns.append(re.compile(line))
        except re.error as error:
            raise PublishError(f"{path}:{number}: bad pattern ({error})") from error
    if not patterns:
        raise PublishError(f"{path} has no patterns")
    return patterns


def literal_hint(pattern):
    """Find text a pattern cannot match without, for a cheap first test.

    The denylist is mostly exact names wrapped in boundary lookarounds, so
    a plain substring test rules most files out far faster than the
    regular expression.

    Args:
        pattern: A compiled denylist pattern.

    Returns:
        str | None: Lowercase text every match contains, or None when the
            pattern is not a simple literal.
    """
    source = re.sub(r"^\(\?[aiLmsux]+\)", "", pattern.pattern)
    source = re.sub(r"^\(\?<!\[[^\]]*\]\)", "", source)
    source = re.sub(r"\(\?!\[[^\]]*\]\)$", "", source)
    if re.search(r"(?<!\\)[][(){}*+?|^$.]", source):
        return None
    return re.sub(r"\\(.)", r"\1", source).lower() or None


def check_snapshot(root, patterns):
    """Find private files and denylisted text in a snapshot.

    Problems name the file, line, and denylist pattern number, never the
    matched text, so the report itself is safe to paste anywhere.

    Args:
        root: The snapshot folder.
        patterns: Compiled denylist patterns.

    Returns:
        list[str]: One line per problem; empty when the snapshot is clean.
    """
    problems = []
    hints = [literal_hint(pattern) for pattern in patterns]
    for relative in snapshot_files(root):
        if any(fnmatch.fnmatch(relative, rule) for rule in FORBIDDEN):
            problems.append(f"{relative}: private file")
            continue
        path = Path(root) / relative
        if path.is_symlink():
            continue
        text = path.read_bytes().decode("utf-8", errors="ignore")
        lowered = f"{relative}\n{text}".lower()
        for index, (pattern, hint) in enumerate(zip(patterns, hints), 1):
            if hint is not None and hint not in lowered:
                continue
            if pattern.search(relative):
                problems.append(f"{relative}: path matches denylist pattern {index}")
            if not pattern.search(text):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    problems.append(f"{relative}:{number}: denylist pattern {index}")
    return problems


def run_gate(snapshot):
    """Run the validation gate against the snapshot's own code.

    The snapshot borrows this checkout's virtualenv, with ``src`` first on
    the import path so the tests exercise the snapshot, not this checkout.

    Args:
        snapshot: The snapshot folder.

    Returns:
        tuple[bool, str]: Whether it passed, and the gate's output.
    """
    venv = Path(snapshot) / ".venv"
    if not venv.exists():
        venv.symlink_to(ROOT / ".venv")
    env = dict(os.environ, PYTHONPATH=str(Path(snapshot) / "src"))
    result = subprocess.run(
        [str(venv / "bin" / "python"), "scripts/gate.py"],
        cwd=snapshot, env=env, capture_output=True, text=True,
    )
    venv.unlink()
    return result.returncode == 0, (result.stdout + result.stderr).strip()


def tree_version(root):
    """Read the version from a tree, requiring every version file to agree.

    Args:
        root: A snapshot folder or working tree.

    Returns:
        str: The version.

    Raises:
        PublishError: When a file lacks it or the files disagree.
    """
    found = {}
    for relative, pattern in VERSION_FILES.items():
        path = Path(root) / relative
        match = pattern.search(path.read_text()) if path.is_file() else None
        if not match:
            raise PublishError(f"no version in {relative}")
        found[relative] = match.group(1)
    if len(set(found.values())) != 1:
        raise PublishError("version files disagree: " + ", ".join(f"{k} {v}" for k, v in found.items()))
    return next(iter(found.values()))


def next_version(version, part):
    """Roll a ``major.minor.patch`` version forward.

    Args:
        version: The released version, such as ``0.5.0``.
        part: ``major``, ``minor``, or ``patch``.

    Returns:
        str: The next version, with lower parts reset to zero.

    Raises:
        PublishError: When the version is not three numbers.
    """
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise PublishError(f"cannot bump {version!r}: expected major.minor.patch")
    numbers = [int(n) for n in version.split(".")]
    index = BUMP_PARTS.index(part)
    numbers[index] += 1
    numbers[index + 1:] = [0] * (2 - index)
    return ".".join(map(str, numbers))


def set_version(root, version):
    """Write a version into every version file of a working tree.

    Args:
        root: The working tree.
        version: The new version.

    Returns:
        None
    """
    for relative, pattern in VERSION_FILES.items():
        path = Path(root) / relative
        text = path.read_text()
        start, end = pattern.search(text).span(1)
        path.write_text(text[:start] + version + text[end:])


def roll_forward(source, ref, released, part):
    """Commit the next version on a new private branch cut from the release.

    Args:
        source: The private repository.
        ref: The released commit or branch.
        released: The version just released.
        part: Which part to bump.

    Returns:
        str: The new branch's name.

    Raises:
        PublishError: When the branch already exists or Git refuses.
    """
    upcoming = next_version(released, part)
    branch = f"version/{upcoming}"
    if has_ref(source, f"refs/heads/{branch}"):
        raise PublishError(f"{branch} already exists in {source}")
    with tempfile.TemporaryDirectory(prefix="labrat-bump-") as temp:
        work = Path(temp) / "work"
        git(source, "worktree", "add", "--quiet", "-b", branch, str(work), ref)
        try:
            set_version(work, upcoming)
            git(work, "commit", "--quiet", "--all", "-m", f"Start {upcoming} after releasing {released}")
        finally:
            git(source, "worktree", "remove", "--force", str(work))
    return branch


def replace_worktree(repo, snapshot):
    """Make a working tree's files exactly the snapshot's, keeping ``.git``.

    Args:
        repo: The public repository's working directory.
        snapshot: The snapshot folder.

    Returns:
        None
    """
    for entry in Path(repo).iterdir():
        if entry.name == ".git":
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    shutil.copytree(snapshot, repo, symlinks=True, dirs_exist_ok=True)


def check_public(public, tag):
    """Refuse to work in a public repository that is not ready.

    Args:
        public: The public repository's working directory.
        tag: The release tag about to be created.

    Returns:
        None

    Raises:
        PublishError: When it has local changes or no Git identity, the
            tag exists, or a branch is behind its remote.
    """
    if git_text(public, "status", "--porcelain"):
        raise PublishError(f"{public} has uncommitted changes; commit or discard them first")
    try:
        git(public, "var", "GIT_COMMITTER_IDENT")
    except PublishError as error:
        raise PublishError(
            f"{public} has no Git identity; set the name and email public commits should carry "
            f"with `git -C {public} config user.name ...` and `user.email ...`"
        ) from error
    if has_ref(public, f"refs/tags/{tag}"):
        raise PublishError(f"tag {tag} already exists in {public}; bump the version")
    remotes = git_text(public, "remote").split()
    if "origin" not in remotes:
        return
    git(public, "fetch", "--quiet", "--tags", "origin")
    for branch in (PUBLIC_BRANCH, SNAPSHOT_BRANCH):
        remote = f"origin/{branch}"
        if has_ref(public, remote) and has_ref(public, branch):
            if git_text(public, "rev-list", "--count", f"{branch}..{remote}") != "0":
                raise PublishError(f"{branch} is behind {remote} in {public}; pull it first")
        elif has_ref(public, remote):
            git(public, "branch", "--quiet", branch, remote)


def commit_snapshot(public, snapshot, version):
    """Commit the snapshot on the snapshot branch and merge it into main.

    On the first release into a repository the host created with a starter
    commit (unrelated history), the snapshot's version of any file both
    have replaces the starter's; files only the starter has are kept and
    listed. Later releases merge normally and stop on a conflict.

    Args:
        public: The public repository's working directory.
        snapshot: The snapshot folder.
        version: The release version.

    Returns:
        bool: True when the merge finished; False when it stopped on a
            conflict for the owner to resolve.

    Raises:
        PublishError: When the release changes nothing.
    """
    first = not has_ref(public, SNAPSHOT_BRANCH)
    if first:
        git(public, "checkout", "--quiet", "--orphan", SNAPSHOT_BRANCH)
    else:
        git(public, "checkout", "--quiet", SNAPSHOT_BRANCH)
    replace_worktree(public, snapshot)
    git(public, "add", "--all")
    if not first and subprocess.run(["git", "-C", str(public), "diff", "--cached", "--quiet"]).returncode == 0:
        git(public, "checkout", "--quiet", PUBLIC_BRANCH)
        raise PublishError("the release branch matches the last published snapshot; nothing to publish")
    git(public, "commit", "--quiet", "-m", f"Snapshot {version}")
    if not has_ref(public, PUBLIC_BRANCH):
        git(public, "branch", PUBLIC_BRANCH, SNAPSHOT_BRANCH)
        git(public, "checkout", "--quiet", PUBLIC_BRANCH)
        return True
    git(public, "checkout", "--quiet", PUBLIC_BRANCH)
    merge = ["merge", "--no-ff", "-m", f"Release {version}", SNAPSHOT_BRANCH]
    starter = subprocess.run(["git", "-C", str(public), "merge-base", PUBLIC_BRANCH, SNAPSHOT_BRANCH],
                             capture_output=True).returncode != 0
    if starter:
        # main holds only what the host created with the repository (a
        # placeholder README, say): the release's version of a shared file wins.
        merge[1:1] = ["--allow-unrelated-histories", "-X", "theirs"]
    result = subprocess.run(["git", "-C", str(public), *merge], capture_output=True, text=True)
    if result.returncode == 0 and starter:
        released = set(git_text(public, "ls-tree", "-r", "--name-only", SNAPSHOT_BRANCH).splitlines())
        kept = sorted(set(git_text(public, "ls-tree", "-r", "--name-only", PUBLIC_BRANCH).splitlines()) - released)
        if kept:
            print("publish: kept from the repository's starter commit: " + ", ".join(kept))
    return result.returncode == 0


def export(args):
    """Build, check, and commit a release in the public repository.

    Args:
        args: Parsed command-line arguments.

    Returns:
        int: The exit status.

    Raises:
        PublishError: When the release branch is missing or a step fails.
    """
    source, public = Path(args.source), Path(args.public)
    ref = args.branch
    if args.remote:
        git(source, "fetch", "--quiet", args.remote, args.branch)
        ref = f"{args.remote}/{args.branch}"
    if not has_ref(source, ref):
        raise PublishError(f"{source} has no {ref}; create the release branch first")
    commit = git_text(source, "rev-parse", "--short", ref)
    patterns = load_denylist(args.denylist)
    with tempfile.TemporaryDirectory(prefix="labrat-release-") as temp:
        snapshot = Path(temp) / "tree"
        snapshot.mkdir()
        export_tree(source, ref, snapshot)
        version = args.version or tree_version(snapshot)
        tag = f"v{version}"
        print(f"publish: {ref} ({commit}) as {tag}, {len(snapshot_files(snapshot))} files")
        changelog = snapshot / "CHANGELOG.md"
        if changelog.is_file() and not re.search(rf"^## {re.escape(version)}\b", changelog.read_text(), re.M):
            raise PublishError(f"CHANGELOG.md has no '## {version}' entry; add one (scripts/changelog.py drafts it)")
        if has_ref(source, f"refs/tags/{tag}"):
            raise PublishError(f"{source} already has tag {tag}; bump the version")
        problems = check_snapshot(snapshot, patterns)
        if problems:
            print("publish: the snapshot is not safe to publish:", *problems, sep="\n  ", file=sys.stderr)
            return 1
        print("publish: no private files or denylisted text")
        if not args.skip_gate:
            ok, output = run_gate(snapshot)
            print("\n".join("  " + line for line in output.splitlines()[-6:]))
            if not ok:
                print("publish: the gate fails on the snapshot", file=sys.stderr)
                return 1
        if args.dry_run:
            print("publish: dry run, public repository untouched")
            return 0
        check_public(public, tag)
        if not commit_snapshot(public, snapshot, version):
            print(
                f"publish: the merge into {PUBLIC_BRANCH} conflicts with public changes. In {public}, "
                f"resolve the conflicts, `git commit`, then `git tag -a {tag} -m 'LabRat {version}'` "
                "and push as below.",
                file=sys.stderr,
            )
            return 1
    git(public, "tag", "-a", tag, "-m", f"LabRat {version}")
    git(source, "tag", "-a", tag, git_text(source, "rev-parse", ref), "-m", f"LabRat {version}, released")
    print(f"publish: tagged the released private commit {tag}; push it with: git -C {source} push origin {tag}")
    push = ["push", "--atomic", "origin", PUBLIC_BRANCH, SNAPSHOT_BRANCH, tag]
    if args.push:
        git(public, *push)
        print(f"publish: pushed {PUBLIC_BRANCH}, {SNAPSHOT_BRANCH}, and {tag}")
    else:
        print(f"publish: {tag} committed in {public}; review it, then: git -C {public} {' '.join(push)}")
    if args.bump != "none":
        branch = roll_forward(source, ref, version, args.bump)
        print(f"publish: {branch} in {source} starts the next version; push it and merge it into main")
    return 0


def bump(args):
    """Roll the version forward on its own, from the release branch.

    Args:
        args: Parsed command-line arguments.

    Returns:
        int: The exit status.
    """
    source = Path(args.source)
    ref = args.branch
    if args.remote:
        git(source, "fetch", "--quiet", args.remote, args.branch)
        ref = f"{args.remote}/{args.branch}"
    with tempfile.TemporaryDirectory(prefix="labrat-version-") as temp:
        export_tree(source, ref, temp)
        released = tree_version(temp)
    branch = roll_forward(source, ref, released, args.part)
    print(f"publish: {branch} in {source} starts the next version; push it and merge it into main")
    return 0


def import_patches(args):
    """Write public commits since the last release as patches for the private repository.

    Args:
        args: Parsed command-line arguments.

    Returns:
        int: The exit status.

    Raises:
        PublishError: When nothing has been published yet.
    """
    public = Path(args.public)
    if not has_ref(public, SNAPSHOT_BRANCH):
        raise PublishError(f"{public} has no {SNAPSHOT_BRANCH}; nothing has been published yet")
    try:
        since = git_text(public, "describe", "--tags", "--abbrev=0", "--match", "v*", PUBLIC_BRANCH)
    except PublishError:
        since = SNAPSHOT_BRANCH
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    written = git_text(public, "format-patch", "--no-merges", "-o", str(out), f"{since}..{PUBLIC_BRANCH}")
    if not written:
        print(f"publish: no public commits since {since}")
        return 0
    count = len(written.splitlines())
    print(f"publish: {count} patch(es) since {since} in {out}")
    print(f"publish: apply them on a private branch with: git am -3 {out}/*.patch")
    return 0


def parse_args(argv):
    """Parse the command line.

    Args:
        argv: Arguments without the program name.

    Returns:
        argparse.Namespace: The parsed arguments.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    out = commands.add_parser("export", help="publish the release branch as a public release")
    out.add_argument("--public", required=True, help="the public repository's local clone")
    out.add_argument("--version", help="release version (default: pyproject.toml's)")
    out.add_argument("--denylist", default=str(DEFAULT_DENYLIST), help="regular expressions never to publish")
    out.add_argument("--skip-gate", action="store_true", help="do not run the gate on the snapshot")
    out.add_argument("--dry-run", action="store_true", help="build and check only; leave the public repo alone")
    out.add_argument("--push", action="store_true", help="push the release after committing it")
    out.add_argument("--bump", choices=(*BUMP_PARTS, "none"), default="patch",
                     help="roll the private version forward afterwards (default: patch)")
    roll = commands.add_parser("bump", help="only roll the private version forward")
    roll.add_argument("--part", choices=BUMP_PARTS, default="patch", help="which part (default: patch)")
    for sub in (out, roll):
        sub.add_argument("--source", default=str(ROOT), help="the private repository (default: this checkout)")
        sub.add_argument("--branch", default="release", help="the private release branch (default: release)")
        sub.add_argument("--remote", default="origin",
                         help="fetch the release branch from this remote first; '' uses the local branch")
    back = commands.add_parser("import", help="export public contributions as patches")
    back.add_argument("--public", required=True, help="the public repository's local clone")
    back.add_argument("--out", required=True, help="folder for the patch files")
    return parser.parse_args(argv)


def main(argv=None):
    """Run a subcommand and report failures in one line.

    Args:
        argv: Arguments without the program name; ``sys.argv`` by default.

    Returns:
        int: The exit status.
    """
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return {"export": export, "import": import_patches, "bump": bump}[args.command](args)
    except PublishError as error:
        print(f"publish: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
