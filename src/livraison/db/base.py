"""Base déclarative et accès asynchrone à PostgreSQL."""
from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from livraison.config import parametres

# Noms de contraintes prévisibles : indispensable pour des migrations Alembic propres.
CONVENTION_NOMS = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=CONVENTION_NOMS)


moteur = create_async_engine(parametres.sqlalchemy_url, pool_pre_ping=True)
fabrique_session = async_sessionmaker(moteur, expire_on_commit=False)


async def obtenir_session():
    """Dépendance FastAPI : une session par requête."""
    async with fabrique_session() as session:
        yield session
