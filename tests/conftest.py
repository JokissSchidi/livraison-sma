"""Fixtures communes.

Les tests qui touchent à la base utilisent une base DÉDIÉE (« <POSTGRES_DB>_test », par exemple
« livraison_test »), créée automatiquement au premier lancement. Ils ne lisent ni ne modifient
jamais vos données de développement.
"""
import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from livraison.config import parametres
from livraison.db import modeles  # noqa: F401  (enregistre les tables dans Base.metadata)
from livraison.db.base import Base

NOM_BASE_TEST = f"{parametres.postgres_db}_test"
URL_BASE_TEST = make_url(parametres.sqlalchemy_url).set(database=NOM_BASE_TEST)
URL_ADMIN = make_url(parametres.sqlalchemy_url).set(database="postgres")


async def _preparer_base_de_test() -> None:
    admin = create_async_engine(URL_ADMIN, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with admin.connect() as cx:
            existe = await cx.scalar(
                text("select 1 from pg_database where datname = :nom"), {"nom": NOM_BASE_TEST}
            )
            if not existe:
                await cx.execute(text(f'CREATE DATABASE "{NOM_BASE_TEST}"'))
    finally:
        await admin.dispose()
    moteur = create_async_engine(URL_BASE_TEST, poolclass=NullPool)
    try:
        async with moteur.begin() as cx:
            await cx.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
            await cx.run_sync(Base.metadata.create_all)  # ne touche pas aux tables existantes
    finally:
        await moteur.dispose()


@pytest.fixture(scope="session")
def base_de_test():
    """Crée (si besoin) la base de test et son schéma ; ignore les tests si PostgreSQL est absent."""
    try:
        asyncio.run(_preparer_base_de_test())
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"PostgreSQL injoignable ou base de test impossible à créer : {type(e).__name__}: {e}")


@pytest_asyncio.fixture
async def session(base_de_test):
    """Session dont toutes les écritures sont annulées à la fin du test."""
    moteur = create_async_engine(URL_BASE_TEST, poolclass=NullPool)
    async with moteur.connect() as cx:
        transaction = await cx.begin()
        # Les « commit » du code testé ne valident que des points de sauvegarde :
        # la transaction externe est annulée à la fin, la base de test reste vide.
        s = AsyncSession(bind=cx, expire_on_commit=False, join_transaction_mode="create_savepoint")
        yield s
        await s.close()
        await transaction.rollback()
    await moteur.dispose()
