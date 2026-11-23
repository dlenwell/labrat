#!/bin/sh
# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0
#
# Container entrypoint for the sample image.
#
#   web        serve the app on port 8000; it shows a holding page at once
#              and migrates the database itself (LABRAT_MIGRATE_ON_START)
#   collector  run the read-only collector (extra arguments pass through,
#              such as --interval 120 or --once)
#   anything else runs as given (for example: alembic -c alembic.ini current)
#
# On first start, with no config yet, it writes one from the example with
# a fresh session key and LABRAT_ALLOWED_HOSTS (comma-separated) as the
# accepted browser hosts, plus the loopback names the health check uses.
# Edit it afterwards like any local_config.toml.
set -eu

config="${LABRAT_CONFIG:-/srv/labrat/config/local_config.toml}"
data_dir="/srv/labrat/data"

# Fail early, with the fix, when a mounted folder is not writable.
for folder in "$(dirname "$config")" "$data_dir"; do
    if [ ! -w "$folder" ]; then
        echo "labrat: $folder is not writable by uid $(id -u); chown it or set LABRAT_UID/LABRAT_GID" >&2
        exit 1
    fi
done

if [ ! -s "$config" ]; then
    # Write beside the target, then rename, so a failed first start never
    # leaves an empty config that later starts would mistake for a real one.
    python - "$config" <<'PY'
import os, secrets, sys
import tomlkit

with open("local_config.example.toml", encoding="utf-8") as handle:
    document = tomlkit.parse(handle.read())
hosts = [item.strip() for item in os.environ.get("LABRAT_ALLOWED_HOSTS", "").split(",") if item.strip()]
# The container's health check reaches the app on loopback.
for loopback in ("localhost", "127.0.0.1", "::1"):
    if loopback not in hosts:
        hosts.append(loopback)
document["server"]["allowed_hosts"] = hosts
document["auth"]["secret_key"] = secrets.token_urlsafe(48)
document["auth"]["secure_cookies"] = os.environ.get("LABRAT_SECURE_COOKIES", "false").lower() == "true"
partial = sys.argv[1] + ".new"
with open(partial, "w", encoding="utf-8") as handle:
    handle.write(tomlkit.dumps(document))
os.chmod(partial, 0o600)
os.replace(partial, sys.argv[1])
print(f"labrat: wrote {sys.argv[1]} with a new session key; allowed hosts: {', '.join(hosts)}")
PY
fi

case "${1:-web}" in
    web)
        [ "$#" -gt 0 ] && shift
        # The startup gate migrates under a lock while the holding page
        # answers, so a long migration never looks like a dead server.
        export LABRAT_MIGRATE_ON_START="${LABRAT_MIGRATE_ON_START:-1}"
        # No control socket: its default path is in the read-only work dir.
        exec gunicorn -c gunicorn.conf.py --bind 0.0.0.0:8000 --workers "${LABRAT_WORKERS:-2}" \
            --no-control-socket \
            --access-logfile - --error-logfile - "$@" labrat.app:app
        ;;
    collector)
        shift
        exec python -m labrat.collector "$@"
        ;;
    *)
        exec "$@"
        ;;
esac
