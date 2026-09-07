"""
Alembic environment.

Connections are built through `app.db.make_engine` rather than Alembic's own
`engine_from_config`, so migrations run with the same `foreign_keys=ON` and
`journal_mode=WAL` pragmas the application uses. A migration that ran with
foreign keys off could create rows the app can never reproduce.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from app.db import db_url, make_engine

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers defaults to True, which silently switches OFF
    # every logger created before the migration ran -- including the whole
    # app.* tree. A process that migrates then serves would lose all its
    # logging, and in tests it makes caplog capture nothing.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# No autogenerate: PLAN.md section C is written out by hand in 0001_baseline,
# and the domain models are deliberately not ORM entities (CLAUDE.md: Core, not
# ORM), so there is no metadata to diff against.
target_metadata = None


def _url() -> str:
    """Test harnesses override this via cfg.set_main_option('sqlalchemy.url', ...)."""
    configured = config.get_main_option("sqlalchemy.url", default=None)
    if configured and configured != "sqlite:///data/app.db":
        return configured
    return db_url()


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = make_engine(_url())
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # SQLite cannot ALTER most things in place
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
