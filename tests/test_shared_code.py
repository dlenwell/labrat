# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Keep the shared-code map honest.

AGENTS.md lists every page, its templates, and its specs. Shared static
files and template partials name the pages that use them in a ``Used by:``
header, and each page spec lists its shared files under ``## Shared code``.
These checks work out real usage from the templates (``extends``,
``from``/``import``/``include``, and ``url_for('static', filename=...)``)
and fail when the table, a header, or a spec's list disagrees with it.

Public releases leave out ``AGENTS.md`` and ``planning/``, so there the
checks skip; they run in the private repository, where both exist.
"""

import fnmatch
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "labrat"
TEMPLATES = PACKAGE / "templates"
STATIC = PACKAGE / "static"
SPECS = ROOT / "planning" / "specs"

EVERY_PAGE = "every page"
"""Header value for files the shared layout (``base.html``) loads."""

_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
_EXTENDS = re.compile(r"""\{%-?\s*extends\s+["']([^"']+)["']""")
_IMPORTS = re.compile(r"""\{%-?\s*(?:from|import|include)\s+["']([^"']+)["']""")
_STATIC = re.compile(r"""url_for\(\s*['"]static['"]\s*,\s*filename\s*=\s*['"]([^'"]+\.(?:js|css))['"]""")
_RENDER = re.compile(r"""render_template\(\s*["']([^"']+)["']""")
_USED_BY = re.compile(r"Used by:\s*(.+?)\s*\.?\s*(?:\*/|#\})?\s*$")
_SPEC_ENTRY = re.compile(r"^-\s+`([^`]+)`:\s*(.+?)\.?\s*$")


def page_table():
    """Parse the page table in AGENTS.md.

    Returns:
        list[tuple[str, list[str], list[str]]]: Page name, template patterns
        (relative to ``templates/``), and spec file names, in table order.
    """
    text = (ROOT / "AGENTS.md").read_text()
    rows = []
    in_table = False
    for line in text.splitlines():
        if line.startswith("| Page | Templates | Specs |"):
            in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                break
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if set(cells[0]) <= {"-"}:
                continue
            rows.append((cells[0], re.findall(r"`([^`]+)`", cells[1]), re.findall(r"`([^`]+)`", cells[2])))
    return rows


def rendered_templates():
    """Return every template name Python code renders as a page.

    Returns:
        set[str]: The template names.
    """
    names = set()
    for path in PACKAGE.rglob("*.py"):
        names.update(_RENDER.findall(path.read_text()))
    return names


def template_source(name):
    """Return a template's text with Jinja comments removed.

    Args:
        name: The template's name, such as ``private/racks.html``.

    Returns:
        str: Its source.
    """
    return _COMMENT.sub("", (TEMPLATES / name).read_text())


def template_closure(name):
    """Return every template a page renders through: itself, layouts, partials.

    Args:
        name: The page template's name.

    Returns:
        set[str]: It and every template it extends, imports, or includes.
    """
    seen = set()
    pending = [name]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        source = template_source(current)
        pending.extend(_EXTENDS.findall(source))
        pending.extend(_IMPORTS.findall(source))
    return seen


def usage_map():
    """Work out which pages use each shared candidate file.

    Returns:
        tuple[dict[str, set[str]], dict[str, list[str]], list[str]]:
        File (relative to ``src/labrat``) to page names; page name to
        template names; all page names in table order.
    """
    rendered = rendered_templates()
    page_templates = {}
    for page, patterns, _specs in page_table():
        page_templates[page] = sorted(
            name for name in rendered if any(fnmatch.fnmatch(name, pattern) for pattern in patterns)
        )
    usage = {}
    for page, names in page_templates.items():
        for name in names:
            for template in template_closure(name):
                if template not in rendered:
                    usage.setdefault(f"templates/{template}", set()).add(page)
                for static in _STATIC.findall(template_source(template)):
                    usage.setdefault(f"static/{static}", set()).add(page)
    return usage, page_templates, list(page_templates)


def expected_label(pages, all_pages):
    """Return the set of names a header should list for these pages.

    Args:
        pages: The pages that use a file.
        all_pages: Every page in the table.

    Returns:
        set[str]: The page names, or ``every page`` when it is all of them.
    """
    return {EVERY_PAGE} if set(pages) == set(all_pages) else set(pages)


def header_pages(path):
    """Return the page names in a file's ``Used by:`` header, or None.

    Args:
        path: The static file or partial.

    Returns:
        set[str] | None: The listed pages.
    """
    for line in path.read_text().splitlines()[:60]:
        match = _USED_BY.search(line)
        if match:
            return {name.strip() for name in match.group(1).split(",") if name.strip()}
    return None


def spec_shared_code(path):
    """Return a spec's ``## Shared code`` entries, or None without a section.

    Args:
        path: The page spec.

    Returns:
        dict | None: Each listed file and its pages.
    """
    lines = path.read_text().splitlines()
    try:
        start = lines.index("## Shared code")
    except ValueError:
        return None
    entries = {}
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        match = _SPEC_ENTRY.match(line)
        if match:
            entries[match.group(1)] = {name.strip() for name in match.group(2).split(",")}
    return entries


@unittest.skipUnless(
    (ROOT / "AGENTS.md").is_file() and SPECS.is_dir(),
    "the page map and specs are private; public releases leave them out",
)
class SharedCodeMapTests(unittest.TestCase):
    """The page table, ``Used by:`` headers, and spec lists match the code."""

    @classmethod
    def setUpClass(cls):
        """Work out real usage once for every check."""
        cls.usage, cls.page_templates, cls.pages = usage_map()

    def test_every_rendered_template_has_one_page(self):
        """A new page must be added to the AGENTS.md table."""
        claimed = {}
        for page, names in self.page_templates.items():
            self.assertTrue(names, f"{page}: no rendered template matches its row")
            for name in names:
                claimed.setdefault(name, []).append(page)
        for name in rendered_templates():
            self.assertEqual(len(claimed.get(name, [])), 1, f"{name}: expected exactly one page row")

    def test_table_specs_exist(self):
        """Every spec the table names is a real file."""
        for page, _patterns, specs in page_table():
            for spec in specs:
                self.assertTrue((SPECS / spec).is_file(), f"{page}: missing planning/specs/{spec}")

    def test_shared_files_carry_a_correct_used_by_header(self):
        """Files used by two or more pages must say which; any header must be right."""
        candidates = [path for path in STATIC.rglob("*") if path.suffix in {".js", ".css"}]
        candidates += [path for path in TEMPLATES.rglob("*.html")]
        rendered = rendered_templates()
        problems = []
        for path in sorted(candidates):
            key = str(path.relative_to(PACKAGE))
            if key.startswith("templates/") and key.removeprefix("templates/") in rendered:
                continue
            pages = self.usage.get(key, set())
            found = header_pages(path)
            expected = expected_label(pages, self.pages)
            if len(pages) >= 2 and found is None:
                problems.append(f"{key}: add 'Used by: {', '.join(p for p in self.pages if p in pages) if expected != {EVERY_PAGE} else EVERY_PAGE}.'")
            elif found is not None and found != expected:
                listed = EVERY_PAGE if expected == {EVERY_PAGE} else ", ".join(p for p in self.pages if p in pages) or "(no page)"
                problems.append(f"{key}: header says {sorted(found)}, should be 'Used by: {listed}.'")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_page_specs_list_their_shared_code(self):
        """Each page spec's Shared code section lists exactly its shared files."""
        spec_pages = {}
        for page, _patterns, specs in page_table():
            for spec in specs:
                spec_pages.setdefault(spec, set()).add(page)
        problems = []
        for spec, pages in sorted(spec_pages.items()):
            expected = {
                key: users
                for key, users in self.usage.items()
                if len(users) >= 2 and set(users) != set(self.pages) and users & pages
            }
            found = spec_shared_code(SPECS / spec)
            if found is None:
                problems.append(f"{spec}: add a '## Shared code' section")
                continue
            for key in sorted(expected.keys() - found.keys()):
                problems.append(f"{spec}: list `{key}`: {', '.join(p for p in self.pages if p in expected[key])}")
            for key in sorted(found.keys() - expected.keys()):
                problems.append(f"{spec}: `{key}` is not shared code of its pages")
            for key in sorted(expected.keys() & found.keys()):
                if found[key] != expected[key]:
                    problems.append(f"{spec}: `{key}` should list {', '.join(p for p in self.pages if p in expected[key])}")
        self.assertEqual(problems, [], "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
