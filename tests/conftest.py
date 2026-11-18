"""Fixtures communes : session de base de données annulée à la fin de chaque test."""
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from livraison.config import parametres


@pytest_asyncio.fixture
async def session():
    moteur = create_async_engine(parametres.sqlalchemy_url, poolclass=NullPool)
    try:
        async with moteur.connect() as cx:
            existantes = (await cx.execute(
                text("select table_name from information_schema.tables where table_schema='public'")
            )).scalars().all()
    except Exception as e:  # noqa: BLE001
        await moteur.dispose()
        pytest.skip(f"PostgreSQL injoignable : {type(e).__name__}")
    if "codes_verification" not in existantes:
        await moteur.dispose()
        pytest.skip("Tables absentes : lancez `alembic upgrade head`")
    async with moteur.connect() as cx:
        transaction = await cx.begin()
        # Les « commit » du code testé ne valident que des points de sauvegarde :
        # la transaction externe est annulée à la fin, la base reste vide.
        s = AsyncSession(bind=cx, expire_on_commit=False, join_transaction_mode="create_savepoint")
        yield s
        await s.close()
        await transaction.rollback()
    await moteur.dispose()
