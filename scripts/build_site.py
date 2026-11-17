#!/usr/bin/env python3
# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Build the project website: the landing page plus the app's own docs.

The website is static. ``site/`` holds the hand-written landing page; the
guides and the API reference are the app's ``/docs`` pages, rendered by the
app itself so the website and the app never disagree. The output folder
(``_site`` by default, ignored by Git) is what GitHub Pages serves.

Every link in the output is rewritten relative to its page, so the site
works at a domain's root, under a path, or opened from disk. A link to an
app page that the website does not have (sign in, Locations) keeps its text
and loses its link. The build fails if any link or image is left pointing
at nothing.

Usage::

    .venv/bin/python scripts/build_site.py                      # into _site
    .venv/bin/python scripts/build_site.py --out /tmp/site --repo-url https://example.invalid/labrat
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SITE_SOURCE = ROOT / "site"
STATIC_SOURCE = ROOT / "src" / "labrat" / "static"
#: The app's docs pages, by URL, and where each goes in the website.
DOCS_PAGES = {
    "/docs": "docs/index.html",
    "/docs/setup": "docs/setup/index.html",
    "/docs/tour": "docs/tour/index.html",
    "/docs/inventory": "docs/inventory/index.html",
    "/docs/discovery": "docs/discovery/index.html",
    "/docs/api": "docs/api/index.html",
}
_ATTRIBUTE = re.compile(r'(?P<name>href|src)="(?P<url>/[^"]*)"')
_APP_LINK = re.compile(r'<a\b(?P<attrs>[^>]*)\bhref="(?P<url>/[^"]*)"(?P<rest>[^>]*)>(?P<text>.*?)</a>', re.S)
_ANY_LINK = re.compile(r'(?:href|src)="([^"#?]+)')
#: Sections of the app's landing page the website reuses, by placeholder:
#: the animated rack hero.
LANDING_SECTIONS = {
    "<!-- app:landing-hero -->": re.compile(r'<section class="landing-hero\b.*?</section>', re.S),
}


class SiteError(Exception):
    """The site cannot be built; the message says why."""


def target_for(url: str) -> str | None:
    """Map an absolute app URL to its file in the website.

    Args:
        url: A path such as ``/docs/tour`` or ``/static/css/main.css``.

    Returns:
        str | None: The website path (``docs/tour/``), or None when the
            website has no such page.
    """
    path, _, fragment = url.partition("#")
    path = path.split("?", 1)[0]
    suffix = f"#{fragment}" if fragment else ""
    if path == "/":
        return "./" + suffix
    if path in DOCS_PAGES:
        return DOCS_PAGES[path].removesuffix("index.html") + suffix
    if path.startswith("/static/") and (STATIC_SOURCE / path.removeprefix("/static/")).is_file():
        return path.lstrip("/") + suffix
    return None


def relative(target: str, page: str) -> str:
    """Express a website path relative to the page that links to it.

    Args:
        target: The linked website path, such as ``static/css/main.css``.
        page: The linking page's website path, such as ``docs/tour/index.html``.

    Returns:
        str: A relative URL, keeping a trailing slash for directories.
    """
    path, _, fragment = target.partition("#")
    base = PurePosixPath(page).parent
    rel = os.path.relpath(path or ".", base or ".")
    if path.endswith("/") or path in ("", "./"):
        rel = rel.rstrip("/") + "/"
    return rel + (f"#{fragment}" if fragment else "")


def rewrite(html: str, page: str) -> str:
    """Point every absolute link in a rendered page at the website's files.

    Args:
        html: The rendered page.
        page: Its website path.

    Returns:
        str: The page with relative links, and app-only links unwrapped.
    """
    def unwrap(match: re.Match) -> str:
        """Keep a link the website can serve; drop the link from one it cannot."""
        if target_for(match["url"]) is not None:
            return match[0]
        return f"<span{match['attrs'].rstrip()}{match['rest']}>{match['text']}</span>"

    html = _APP_LINK.sub(unwrap, html)

    def point(match: re.Match) -> str:
        """Rewrite one href or src."""
        target = target_for(match["url"])
        if target is None:
            raise SiteError(f"{page}: {match['name']} {match['url']} has no page in the website")
        return f'{match["name"]}="{relative(target, page)}"'

    return _ATTRIBUTE.sub(point, html)


def render_docs(out: Path, repo_url: str | None) -> None:
    """Render the app's docs pages, and its landing hero, into the website.

    The landing page (``site/index.html``) marks where the app's animated
    rack hero goes; they are rendered from the app's own
    template so the two never drift apart.

    Args:
        out: The output folder.
        repo_url: The public repository's address for the header, if any.

    Raises:
        SiteError: When a page fails to render.
    """
    with tempfile.TemporaryDirectory(prefix="labrat-site-") as temp:
        config = Path(temp) / "local_config.toml"
        config.write_text(
            f'[database]\npath = "{Path(temp) / "site.sqlite3"}"\n'
            f'[auth]\nsecret_key = "{"site-build-" + "x" * 40}"\n'
        )
        previous = os.environ.get("LABRAT_CONFIG")
        os.environ["LABRAT_CONFIG"] = str(config)
        try:
            from labrat.app import create_app  # Reads LABRAT_CONFIG.

            app = create_app()
        finally:
            if previous is None:
                os.environ.pop("LABRAT_CONFIG", None)
            else:
                os.environ["LABRAT_CONFIG"] = previous
        app.jinja_env.globals["site_build"] = {"repo_url": repo_url}
        client = app.test_client()
        with app.test_request_context("/"):
            from flask import render_template

            landing = render_template("public/index.html", needs_setup=False)
        index = out / "index.html"
        page_html = index.read_text(encoding="utf-8")
        for placeholder, pattern in LANDING_SECTIONS.items():
            found = pattern.search(landing)
            if found is None or placeholder not in page_html:
                raise SiteError(f"cannot fill {placeholder} from the app's landing page")
            page_html = page_html.replace(placeholder, found[0])
        index.write_text(rewrite(page_html, "index.html"), encoding="utf-8")
        for url, page in DOCS_PAGES.items():
            response = client.get(url)
            if response.status_code != 200:
                raise SiteError(f"{url} returned HTTP {response.status_code}")
            path = out / page
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rewrite(response.get_data(as_text=True), page), encoding="utf-8")
        app.extensions["labrat_engine"].dispose()


def check_links(out: Path) -> list[str]:
    """Find relative links and images in the website that point at nothing.

    Args:
        out: The built website.

    Returns:
        list[str]: One line per broken reference.
    """
    broken = []
    for page in sorted(out.rglob("*.html")):
        for url in _ANY_LINK.findall(page.read_text(encoding="utf-8")):
            if re.match(r"[a-z]+:|//|mailto:", url):
                continue
            target = (page.parent / url).resolve()
            if url.endswith("/"):
                target = target / "index.html"
            if not target.exists():
                broken.append(f"{page.relative_to(out)}: {url}")
    return broken


def build(out: Path, repo_url: str | None) -> int:
    """Build the website into a fresh folder.

    Args:
        out: The output folder; replaced if it exists.
        repo_url: The public repository's address, if any.

    Returns:
        int: How many HTML pages it wrote.

    Raises:
        SiteError: When a page fails or a link is broken.
    """
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(SITE_SOURCE, out)
    shutil.copytree(STATIC_SOURCE, out / "static")
    render_docs(out, repo_url)
    if repo_url:
        for page in out.glob("*.html"):
            page.write_text(page.read_text(encoding="utf-8").replace("{{ repo_url }}", repo_url), encoding="utf-8")
    broken = check_links(out)
    if broken:
        raise SiteError("broken links:\n  " + "\n  ".join(broken))
    return len(list(out.rglob("*.html")))


def main(argv: list[str] | None = None) -> int:
    """Build the website and report the result in one line.

    Args:
        argv: Arguments without the program name; ``sys.argv`` by default.

    Returns:
        int: The exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(ROOT / "_site"), help="output folder (default: _site)")
    parser.add_argument("--repo-url", help="the public repository's address, for the Source links")
    args = parser.parse_args(argv)
    try:
        pages = build(Path(args.out), args.repo_url)
    except SiteError as error:
        print(f"build_site: {error}", file=sys.stderr)
        return 1
    print(f"build_site: {pages} pages in {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
