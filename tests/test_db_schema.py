"""Tests du schéma de base de données.

Prérequis : `docker compose up -d` puis `alembic upgrade head`.
Les tests s'ignorent d'eux-mêmes si PostgreSQL n'est pas joignable.
"""
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from livraison.config import parametres
from livraison.db.base import Base
from livraison.db.modeles import (
    Commande,
    EtatCommande,
    Livreur,
    Role,
    Utilisateur,
    Vehicule,
)

TABLES_ATTENDUES = {
    "utilisateurs", "livreurs", "commandes", "positions",
    "offres", "paiements", "factures", "evenements",
}


def test_metadata_contient_les_tables_du_cahier_des_charges():
    assert TABLES_ATTENDUES <= set(Base.metadata.tables)


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
    if not TABLES_ATTENDUES <= set(existantes):
        await moteur.dispose()
        pytest.skip("Tables absentes : lancez `alembic upgrade head`")
    async with moteur.connect() as cx:
        transaction = await cx.begin()
        s = AsyncSession(bind=cx, expire_on_commit=False)
        yield s
        await s.close()
        await transaction.rollback()  # rien n'est conservé en base
    await moteur.dispose()


async def test_creer_commande_avec_positions_gps(session):
    client = Utilisateur(telephone="+2250700000001", nom="Awa", role=Role.CLIENT)
    session.add(client)
    await session.flush()
    commande = Commande(
        client_id=client.id,
        adresse_enlevement="Plateau",
        position_enlevement="SRID=4326;POINT(-4.0197 5.3236)",
        adresse_livraison="Cocody",
        position_livraison="SRID=4326;POINT(-3.9870 5.3480)",
        poids_kg=2.5,
    )
    session.add(commande)
    await session.flush()
    assert commande.etat == EtatCommande.CREEE

    # Distance à vol d'oiseau calculée par PostGIS (en mètres)
    distance = await session.scalar(
        select(func.ST_Distance(Commande.position_enlevement, Commande.position_livraison))
    )
    assert 3000 < distance < 6000


async def test_telephone_unique(session):
    session.add(Utilisateur(telephone="+2250700000002", nom="A", role=Role.CLIENT))
    await session.flush()
    session.add(Utilisateur(telephone="+2250700000002", nom="B", role=Role.CLIENT))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_livreur_lie_a_un_utilisateur(session):
    u = Utilisateur(telephone="+2250700000003", nom="Koffi", role=Role.LIVREUR)
    session.add(u)
    await session.flush()
    session.add(Livreur(utilisateur_id=u.id, vehicule=Vehicule.MOTO, capacite_kg=20))
    await session.flush()
    livreur = await session.get(Livreur, u.id)
    assert livreur.validation.value == "en_attente"
