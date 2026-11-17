# Copyright 2026 LabRat contributors
# SPDX-License-Identifier: Apache-2.0

"""Configure Alembic against LabRat's local SQLite database and metadata.

Database location is resolved through the same local configuration loader used
by the application. There is intentionally no database URL in alembic.ini, so
credentials and host-specific paths do not become part of source control.
"""

from logging.config import fileConfig

from alembic import context

from labrat import models  # noqa: F401  # Register initial model tables.
from labrat import audit, metadata, placement  # noqa: F401  # Register new inventory tables.
from labrat.discovery import models as discovery_models  # noqa: F401
from labrat.config import load_local_config
from labrat.database import Base, make_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Generate SQL for migrations without connecting to the database."""
    engine = make_engine(load_local_config())
    try:
        context.configure(
            url=str(engine.url),
            target_metadata=target_metadata,
            literal_binds=True,
            dialect_opts={"paramstyle": "named"},
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    finally:
        engine.dispose()


def run_migrations_online() -> None:
    """Run migrations through a configured SQLite database connection."""
    engine = make_engine(load_local_config())
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=True,
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
