# LabRat device groups and capabilities

A physical device can serve several purposes. A router/firewall with an
integrated switch should not have to choose one type just to be displayed.
LabRat therefore keeps its optional `device_classes` classification and adds
**user-defined groups** that are many-to-many with canonical devices.

## New tables (migration `0003_device_groups`)

- `groups`: stable integer ID, case-insensitive unique name, optional description.
  No fixed vocabulary or migration is required to add a group.
- `device_groups`: composite primary key `(device_id, group_id, source)` with
  foreign keys to `devices` and `groups`. Its secondary `group_id` index helps
  reverse filtering. A timestamp records when the assertion was created.

One device may belong to several groups at once, for example `router`,
`switch`, and `firewall`.

## Current implementation status

The tables and ORM models are present, but this build has no group-management
page, JSON API, or collector assignment workflow. Group assertions are not
currently created by LabRat's UI or adapters. Treat this as a schema foundation
for future reviewed work, not as a supported inventory editor.

## Who owns an assertion?

- `source = "manual"` is reserved for a person deliberately assigning the group.
- `source = "unifi:default"` or another stable integration identifier means
  that collector reported the capability.
- The same device may hold the same group once for each source. A collector
  should remove or update **only its own source** and must never remove manual
  assertions. Multiple source claims should be deduplicated for display using
  `DISTINCT groups.id`.

The source string is not an access-control rule. Any future API or collector
must validate actor permissions and protect manually managed membership.

## Canonical inventory versus discovery

Discovery rows remain source-scoped observations rather than canonical
`devices`. The group tables attach to **canonical inventory devices** only.
Discovery categories remain provisional. Any future assignment workflow must
preserve manual assertions and keep source provenance separate.

## Auditing and deletion

Any future group membership writes must call
`labrat.audit.record_audit_event()` in the same transaction, recording the
actor and changed group ID/name without recording secrets. Removing a canonical
device or a group cascades to its memberships; it does not remove the other
entity. Downgrading below `0003` deletes both new tables and their data.

## Query examples

Find distinct capabilities for one canonical device:

```sql
SELECT DISTINCT g.id, g.name
FROM groups AS g
JOIN device_groups AS dg ON dg.group_id = g.id
WHERE dg.device_id = :device_id
ORDER BY g.name;
```

Find all devices in a named group:

```sql
SELECT DISTINCT d.id, d.name
FROM devices AS d
JOIN device_groups AS dg ON dg.device_id = d.id
JOIN groups AS g ON g.id = dg.group_id
WHERE g.name = :group_name;
```

To inspect where a group originated, query `device_groups.source` rather than
just the consolidated group list.
