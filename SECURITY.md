# Security policy

## Reporting a vulnerability

Please report security problems privately, not in a public issue or pull
request. On the project's GitHub page, open the **Security** tab and choose
**Report a vulnerability**. That opens a private advisory that only the
maintainers can see.

Include what you can of:

- what the problem is and what an attacker could do with it;
- the LabRat version or commit, and how it was deployed (directly, behind
  a reverse proxy, or in the sample container);
- steps to reproduce, or a proof of concept.

Leave out your real credentials, addresses, and anything else about your
own network; invented values are fine for a reproduction.

You will get a reply as soon as practical. Fixes land in a new release,
and the advisory is published once that release is out, crediting you
unless you would rather stay anonymous.

## Supported versions

Only the latest release receives security fixes.

## How LabRat is meant to run

Knowing the intended deployment helps judge a report:

- LabRat is a self-hosted app for a home or lab network. It is not
  designed to face the internet directly; put it behind HTTPS and a
  reverse proxy or VPN if it must be reachable from elsewhere.
- Integrations use read-only credentials, and the collector only reads.
  It never logs in to a device to change it or sends it commands.
- Credentials live in `local_config.toml`, never in the database. An
  administrator's whole-install export (to move LabRat) includes them and
  every account's password hash; the Settings page warns before download.
- Beyond the controllers it is configured to read, the collector only
  contacts addresses inside the networks an administrator allows in
  Settings.

A way around any of these (writing through an integration, reading
credentials through the web app or a backup, or making the collector
contact an address it was not given) is a vulnerability worth reporting.
