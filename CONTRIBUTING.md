# Contributing to LabRat

Thanks for helping. LabRat is a local-first homelab discovery and inventory
app: a Flask web app and a separate collector that reads your controllers.
This guide covers getting set up, the checks every change must pass, and
the conventions the code follows.

## Set up

You need Python 3.11 or newer, Git, and Node.js (for the JavaScript checks).

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
cp local_config.example.toml local_config.toml   # then set [auth] secret_key
.venv/bin/alembic -c alembic.ini upgrade head
LABRAT_RELOAD=1 .venv/bin/gunicorn -c gunicorn.conf.py labrat.app:app   # reloads on code changes
```

The [setup guide](docs/setup.md) explains the configuration, and the
collector runs on its own with `python -m labrat.collector --once`.

For screenshots or a lab-free tryout, `scripts/demo_seed.py` builds a
self-contained demo instance with invented names and documentation-range
addresses. Docs and README screenshots come only from it:

```sh
.venv/bin/python scripts/demo_seed.py /path/to/demo
LABRAT_CONFIG=/path/to/demo/local_config.toml .venv/bin/gunicorn -b 127.0.0.1:8010 labrat.app:app
```

Sign in as `demo` with the password in `admin.txt`, take screenshots right
after seeding while the data is fresh, and save them as WebP under
`src/labrat/static/img/docs/`.

## The website

`site/` is the plain-HTML landing page for labrat.foo. The build adds the
app's own landing hero and its docs pages, rendered by the app, so they
never drift apart:

```sh
.venv/bin/python scripts/build_site.py      # writes _site/, fails on a broken link
.venv/bin/python -m http.server 8000 --directory _site
```

## The gate

Every change passes the validation gate before it is proposed:

```sh
.venv/bin/python scripts/gate.py
```

It runs the Python tests under coverage (90% minimum), the JavaScript
checks in `tests/js`, the docstring checks, and Alembic's model/migration
drift check, and prints one line per part ending in `gate: PASS` or
`gate: FAIL`. A change that touches only Markdown can run
`scripts/gate.py --docs`. Continuous integration runs the same gate on
the oldest and newest supported Python, checks the dependencies for
published advisories, and builds and starts the container sample, on
every pull request. Tests use temporary SQLite
databases; none touch your configuration or a real lab. For anything visible, also check
it in a browser, in both the light and dark themes and at phone width.

## Conventions

**Code**

- Every Python module, class, and function has a PEP 257 docstring in
  Google style (`Args:`, `Returns:`, `Yields:`, `Raises:`); every
  JavaScript function has JSDoc (`@param`, `@returns`). The gate enforces
  this; `scripts/doc_tags.py` lists what is missing.
- HTML lives in templates, never in Python. CSS stays expanded and
  commented for people to edit.
- Share rather than copy. When a second page needs the same behavior or
  look, move it into a shared module, stylesheet, or template macro. A
  shared file names its users in a `Used by:` header at the top; keep it
  current, and check every page it lists when you change it.
- Values read from sources or stored JSON go through `labrat/coerce.py`.
  Never import another module's `_private` name; make it public where it
  belongs.
- Front-end scripts call the JSON API through `labratJson` with an
  address from `LABRAT_API` (both in `main.js`), never a hand-written
  fetch. Lines between devices get their paths from `wiring.js`; charts,
  bars, and stat tiles are widgets (`widgets.js` and the `_widgets.html`
  macros).
- Keep changes small and reviewable, and explain a large refactor before
  starting one.

**Data and schema**

- SQLAlchemy models and Alembic migrations own the schema; Flask never
  creates tables. Add a migration only when the existing schema cannot
  represent the feature, and preserve existing inventory, discovery, and
  audit rows.
- Discovered devices, ports, and links are source observations; they
  never set canonical identity or physical placement. Only an explicit
  user action places or links equipment, and a user's name for a device
  is never overwritten by a scan.
- Validate user writes, protect them with CSRF, and record an audit
  event.
- Integrations are read-only. An adapter only reads its source; it never
  logs in to a device to change it or sends it commands.

**Privacy**

- Never commit credentials, hostnames, IP addresses, or details of your
  own lab. Local settings and secrets go in `local_config.toml`, which Git
  ignores; examples use invented names and documentation-range addresses
  (`192.0.2.0/24`, `example.lan`).
- Screenshots come from the demo instance, never a real lab.

## Commits and pull requests

- One logical change per commit, with a message that says what changed
  and why. Describe behavior generically ("a switch uplink on a port
  reported down"), not in terms of your own equipment.
- Open a pull request against `main` with what changed and how you
  tested it. Run the gate first and include its result.
- LabRat is developed in a separate upstream repository and published
  here at each release, so accepted pull requests are carried upstream
  with your authorship and come back in the next release.

## License

By contributing, you agree that your contributions are licensed under the
Apache License 2.0, the same license as the project (`LICENSE`).
