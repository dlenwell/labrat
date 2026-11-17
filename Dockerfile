# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0
#
# A sample LabRat image: one image runs the web app or the collector.
#
#   docker build -t labrat .
#   docker run --rm -p 8000:8000 -v "$PWD/deploy/data:/srv/labrat/data" \
#       -v "$PWD/deploy/config:/srv/labrat/config" labrat
#
# See deploy/compose.yaml for both processes together, and docs/setup.md's
# "Run in containers" section for first-run notes.

# Match the Python version the test gate runs on.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LABRAT_CONFIG=/srv/labrat/config/local_config.toml

WORKDIR /srv/labrat

# Install the package first, so code changes reuse the dependency layer
# only when pyproject.toml is unchanged.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip install . \
    && rm -rf src build

# Migrations run from the working directory; the database path in the
# generated config ("data/labrat.sqlite3") resolves against it too.
COPY alembic.ini local_config.example.toml ./
COPY migrations ./migrations
COPY deploy/entrypoint.sh /usr/local/bin/labrat-entrypoint

# An unprivileged user owns the data and config volumes.
RUN useradd --system --uid 1000 --home-dir /srv/labrat --shell /usr/sbin/nologin labrat \
    && mkdir -p data config \
    && chown labrat:labrat data config \
    && chmod 0755 /usr/local/bin/labrat-entrypoint
USER labrat

VOLUME ["/srv/labrat/data", "/srv/labrat/config"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4)" || exit 1

ENTRYPOINT ["labrat-entrypoint"]
CMD ["web"]
