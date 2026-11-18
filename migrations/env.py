"""Environnement Alembic (mode asynchrone, PostGIS)."""
import asyncio
from logging.config import fileConfig

from alembic import context
from geoalchemy2 import alembic_helpers
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from livraison.config import parametres
from livraison.db import modeles  # noqa: F401  (enregistre les tables dans Base.metadata)
from livraison.db.base import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def options_communes() -> dict:
    return {
        "target_metadata": target_metadata,
        "include_object": alembic_helpers.include_object,
        "process_revision_directives": alembic_helpers.writer,
        "render_item": alembic_helpers.render_item,
        "compare_type": True,
    }


def run_migrations_offline() -> None:
    context.configure(
        url=parametres.sqlalchemy_url,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        **options_communes(),
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, **options_communes())
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    moteur = create_async_engine(parametres.sqlalchemy_url, poolclass=pool.NullPool)
    async with moteur.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await moteur.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
