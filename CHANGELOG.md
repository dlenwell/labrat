# Changelog

What each release adds, newest first. Entries start as a draft from
`scripts/changelog.py` and are kept short.

## 0.5.4 (2026-11-23)

### Added

- **OPNsense integration** (read-only): the firewall with its NICs and
  health; its networks and VLANs with DHCP (dnsmasq or Kea), WAN gateways,
  firewall rules and zones, WireGuard peers, services, CARP, and DNS
  blocklist on Networks; the clients it has seen in Discovery (Seen by
  router, or Lease only); and Observations watchlist alarms for a gateway
  down, a stale VPN peer, or a CARP state change. Its API user needs only
  the privileges the Discovery guide lists.
- Networks shows one router at a time, with a picker when several report a
  network, and Pi-holes appear there before any UniFi snapshot.
- **Settings → API tokens**: scoped tokens for programs, and a standalone
  MikroTik (MNDP) listener that reports from another network segment with
  one.
- Users can change their password from Profile.
- Home's link cards include Cluster and Networks.
- Each release publishes a multi-architecture container image to ghcr.io.

### Changed

- One web server and one collector per database: a second server shows a
  holding page that can take over, step aside, or fork the database, and a
  one-off `collector --once` can run beside the running collector.
- Discovery's map leaves out unplugged, unlinked devices and draws Wi-Fi
  links as visible dashes.
- A wired NUT server's cable replaces the client feed's copy of it.
- The guides and website cover OPNsense (with the privileges it needs),
  API tokens, and the standalone MNDP listener.

### Fixed

- Password-protected Pi-hole v6 is read correctly, and a Pi-hole credential
  is only ever sent to a confirmed Pi-hole.
- Observations no longer warns about network clients that come and go;
  one you added to inventory still warns when it goes quiet.
- A server NIC the UniFi controller files behind the server's own LLDP
  chassis keeps its cable to the switch port that hears it.
- The API reference shows token-only routes as needing an API token, not
  an administrator session.

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
