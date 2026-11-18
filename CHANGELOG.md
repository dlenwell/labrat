# Changelog

What each release adds, newest first. Entries start as a draft from
`scripts/changelog.py` and are kept short.

## 0.5.3 (2026-11-17)

### Added

- **Settings → Export and import** moves a whole install to a new host:
  one file with the database and integrations (credentials included),
  imported as a full replace. It replaces the inventory-only backup, so
  older JSON backups cannot be imported.
- ZFS pools show scrub and resilver progress with the time left, and the
  Storage tab refreshes itself.
- A sample Kubernetes manifest (`deploy/kubernetes.yaml`) and notes on
  running there, including layer-2 discovery.
- Certificate settings can turn checks off for a controller reached by IP
  address with a self-signed certificate.

### Changed

- Containers collect every 60 seconds by default.
- Settings no longer links the Widgets gallery; its address still works.

### Fixed

- A deleted integration no longer lingers in the header or holds on to its
  name: deleting removes what it discovered (inventory is kept), and a
  migration clears integrations deleted earlier.

## 0.5.2 (2026-11-16)

### Changed

- The container image runs Python 3.15.
- Gunicorn 27, plus newer minimum versions of pysnmp, SQLAlchemy, Alembic, and argon2-cffi.

## 0.5.1 (2026-11-16)

First public release.
