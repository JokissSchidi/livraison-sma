"""Reprise après redémarrage (F-36, NF-04) : la plateforme est VRAIMENT arrêtée puis relancée au milieu des scénarios.

Nécessite PostgreSQL et XMPP (voir conftest.py et test_agents.py).
"""
import asyncio
import dataclasses
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from conftest import URL_BASE_TEST
from httpx import ASGITransport, AsyncClient
from outils import FauxMoteur, pt
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from test_agents import comptes  # noqa: F401  (fixture)
from test_api_commandes import COMMANDE, REGL, TABLES, Systeme, gps

from livraison.agents.dispatcher import Statut
from livraison.agents.plateforme import Plateforme
from livraison.agents.supervision import ReglagesSupervision
from livraison.api.main import app
from livraison.config import parametres
from livraison.db.base import obtenir_session
from livraison.db.modeles import Commande, EtatCommande, Livreur, Position, Role


class Reprise(Systeme):
    def __init__(self, api, fabrique, comptes_, supervision=None):
        super().__init__(api, fabrique, None)
        self._comptes, self._supervision = comptes_, supervision

    async def demarrer(self):
        self.plateforme = Plateforme(self.fabrique, comptes=self._comptes, itineraires=FauxMoteur(), reglages=REGL,
                                     prefixe_jid="t7-", supervision=self._supervision)
        await self.plateforme.demarrer()
        app.state.plateforme = self.plateforme

    async def redemarrer(self):
        """Arrêt complet (agents, file d'événements) puis nouveau démarrage : tout est reconstruit depuis la base."""
        await self.plateforme.arreter()
        app.state.plateforme = None
        await self.demarrer()


@pytest_asyncio.fixture
async def reprise(base_de_test, comptes, monkeypatch):  # noqa: F811
    monkeypatch.setattr(parametres, "jwt_secret", "x" * 40)
    moteur = create_async_engine(URL_BASE_TEST, poolclass=NullPool)
    fabrique = async_sessionmaker(moteur, expire_on_commit=False)
    async with moteur.begin() as cx:
        await cx.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))

    async def session_de_requete():
        async with fabrique() as s:
            yield s

    app.dependency_overrides[obtenir_session] = session_de_requete
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
            banc = Reprise(api, fabrique, comptes)
            await banc.demarrer()
            yield banc
    finally:
        app.dependency_overrides.clear()
        app.state.plateforme = None
        if banc.plateforme:
            await banc.plateforme.arreter()
        async with moteur.begin() as cx:
            await cx.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
        await moteur.dispose()


def meme_point(a, b) -> bool:
    return abs(a.latitude - b.latitude) < 1e-5 and abs(a.longitude - b.longitude) < 1e-5


async def attendre_suivi(s, cid):
    """La reprise relance les attributions en arrière-plan : on attend que le Dispatcher ait repris la commande."""
    await s.attendre(lambda: asyncio.sleep(0, result=s.plateforme.dispatcher.suivis.get(cid)), lambda x: x is not None)
    return s.plateforme.dispatcher.suivis[cid]


async def etat_en_base(s, cid):
    async with s.fabrique() as session:
        return (await session.get(Commande, uuid.UUID(cid))).etat


# ----------------------------------------------------------------------------- commandes en recherche

async def test_une_commande_en_recherche_reprend_apres_redemarrage(reprise):
    s = reprise
    _, client = await s.utilisateur(Role.CLIENT, "Awa")
    id1, l1 = await s.utilisateur(Role.LIVREUR, "Koffi")
    cid = (await s.api.post("/commandes", json=COMMANDE, headers=client)).json()["id"]  # aucun livreur en ligne
    await s.attendre(lambda: s.commande(cid, client), lambda c: c.get("etat") == "en_recherche")

    await s.redemarrer()  # le serveur redémarre

    await attendre_suivi(s, cid)
    await s.attendre(lambda: asyncio.sleep(0, result=s.plateforme.dispatcher.en_file()), lambda f: f == [cid])
    await s.connecter(l1, pt(1, 0))  # un livreur arrive : la commande reprend sans perte
    await s.attendre(lambda: s.propositions(l1), lambda p: len(p) == 1)
    await s.api.post(f"/livreurs/moi/propositions/{cid}/accepter", headers=l1)
    final = await s.attendre(lambda: s.commande(cid, client), lambda c: c.get("etat") == "attribuee")
    assert final["livreur_id"] == str(id1)


async def test_commande_creee_juste_avant_l_arret_est_reprise(reprise):
    s = reprise
    client_id, _ = await s.utilisateur(Role.CLIENT, "Awa")
    async with s.fabrique() as session:  # commande enregistrée, mais l'attribution n'avait pas eu le temps de partir
        c = Commande(client_id=client_id, adresse_enlevement="Plateau", position_enlevement="SRID=4326;POINT(-4.0197 5.3236)",
                     adresse_livraison="Cocody", position_livraison="SRID=4326;POINT(-3.987 5.348)", poids_kg=2,
                     priorite=1, etat=EtatCommande.CREEE)
        session.add(c)
        await session.commit()
        cid = str(c.id)
    await s.redemarrer()
    suivi = await attendre_suivi(s, cid)
    assert suivi.urgente is True and suivi.demande.poids_kg == 2.0  # priorité et poids retrouvés
    assert suivi.demande.enlevement.latitude == pytest.approx(5.3236, abs=1e-6)
    await s.attendre(lambda: asyncio.sleep(0, result=suivi.statut), lambda x: x == Statut.EN_FILE)
    await s.plateforme.depot.vider()
    assert await etat_en_base(s, cid) == EtatCommande.EN_RECHERCHE


async def test_les_commandes_terminees_ne_sont_pas_reprises(reprise):
    s = reprise
    client_id, _ = await s.utilisateur(Role.CLIENT, "Awa")
    ids = []
    async with s.fabrique() as session:
        for etat in (EtatCommande.LIVREE, EtatCommande.CLOTUREE, EtatCommande.ANNULEE, EtatCommande.ECHEC_LIVRAISON):
            c = Commande(client_id=client_id, adresse_enlevement="A", position_enlevement="SRID=4326;POINT(-4 5.3)",
                         adresse_livraison="B", position_livraison="SRID=4326;POINT(-4 5.4)", poids_kg=1, etat=etat)
            session.add(c)
            await session.flush()
            ids.append(str(c.id))
        await session.commit()
    await s.redemarrer()
    assert not any(i in s.plateforme.dispatcher.suivis for i in ids)


# ----------------------------------------------------------------------------- commandes attribuées

async def attribuer(s, client, livreur, position):
    await s.connecter(livreur, position)
    cid = (await s.api.post("/commandes", json=COMMANDE, headers=client)).json()["id"]
    await s.attendre(lambda: s.propositions(livreur), lambda p: len(p) == 1)
    await s.api.post(f"/livreurs/moi/propositions/{cid}/accepter", headers=livreur)
    await s.attendre(lambda: s.commande(cid, client), lambda c: c.get("etat") == "attribuee")
    return cid


async def test_livreur_et_course_attribuee_retrouves_apres_redemarrage(reprise):
    s = reprise
    _, client = await s.utilisateur(Role.CLIENT, "Awa")
    id1, l1 = await s.utilisateur(Role.LIVREUR, "Koffi")
    cid = await attribuer(s, client, l1, pt(1, 0))

    await s.redemarrer()

    # L'agent du livreur est reconstruit avec sa course (poids, arrêts), à partir de la base.
    agent = s.plateforme.agent_livreur(str(id1))
    assert agent is not None
    sit = agent.situation
    assert sit.nb_courses == 1 and sit.charge_kg == 2.0
    assert len(sit.arrets_restants) == 2
    assert meme_point(sit.arrets_restants[0], pt(0, 0)) and meme_point(sit.arrets_restants[1], pt(0, 5))  # enlèvement puis livraison
    assert cid in agent.courses
    # Le Dispatcher retrouve la commande, attribuée à ce livreur.
    suivi = s.plateforme.dispatcher.suivis[cid]
    assert suivi.statut == Statut.ATTRIBUEE and suivi.attribution.livreur_id == str(id1)
    # Le téléphone n'a rien à refaire : sa position est acceptée sans reconnexion.
    r = await s.api.post("/livreurs/moi/position", json=gps(pt(1, 1)), headers=l1)
    assert r.status_code == 200
    assert (await s.commande(cid, client))["etat"] == "attribuee"
    # Et la commande peut toujours être annulée (le Dispatcher la connaît) : le livreur est libéré.
    assert (await s.api.post(f"/commandes/{cid}/annuler", headers=client)).json()["etat"] == "annulee"
    await s.attendre(lambda: asyncio.sleep(0, result=agent.situation.nb_courses), lambda n: n == 0)


async def test_colis_deja_enleve_seule_la_livraison_reste_a_faire(reprise):
    s = reprise
    _, client = await s.utilisateur(Role.CLIENT, "Awa")
    id1, l1 = await s.utilisateur(Role.LIVREUR, "Koffi")
    cid = await attribuer(s, client, l1, pt(1, 0))
    async with s.fabrique() as session:
        await session.execute(update(Commande).where(Commande.id == uuid.UUID(cid)).values(etat=EtatCommande.ENLEVEE))
        await session.commit()
    await s.redemarrer()
    sit = s.plateforme.agent_livreur(str(id1)).situation
    assert len(sit.arrets_restants) == 1 and sit.nb_courses == 1 and sit.charge_kg == 2.0
    assert meme_point(sit.arrets_restants[0], pt(0, 5))  # reste la livraison


async def test_livreur_hors_ligne_garde_sa_course_et_sera_remplace_si_silencieux(reprise):
    s = reprise
    _, client = await s.utilisateur(Role.CLIENT, "Awa")
    id1, l1 = await s.utilisateur(Role.LIVREUR, "Koffi")
    cid = await attribuer(s, client, l1, pt(1, 0))
    await s.api.post("/livreurs/moi/deconnexion", headers=l1)  # hors ligne (en_ligne = False), course en cours
    await s.redemarrer()
    assert s.plateforme.agent_livreur(str(id1)) is None  # pas de téléphone en ligne : pas d'agent
    assert s.plateforme.dispatcher.suivis[cid].statut == Statut.ATTRIBUEE  # mais la commande n'est pas perdue
    sup = s.plateforme.superviseur
    sup.reglages = dataclasses.replace(sup.reglages, alerte_apres_s=0.2, reattribution_apres_s=0.5)
    assert await sup.passe() == []
    await asyncio.sleep(0.6)
    assert await sup.passe() == [(cid, "alerte"), (cid, "reattribution")]
    await s.attendre(lambda: s.commande(cid, client), lambda c: c.get("etat") == "en_recherche")


async def test_le_journal_garde_trace_de_la_reprise(reprise):
    s = reprise
    _, client = await s.utilisateur(Role.CLIENT, "Awa")
    _, admin = await s.utilisateur(Role.ADMINISTRATEUR, "Admin")
    cid = (await s.api.post("/commandes", json=COMMANDE, headers=client)).json()["id"]
    await s.attendre(lambda: s.commande(cid, client), lambda c: c.get("etat") == "en_recherche")
    await s.redemarrer()
    await s.plateforme.depot.vider()
    types = [e["type"] for e in (await s.commande(cid, admin))["evenements"]]
    assert "reprise" in types and types.count("soumise") == 2  # soumise avant l'arrêt, puis de nouveau après


# ----------------------------------------------------------------------------- nettoyage des livreurs inactifs

async def test_un_livreur_en_ligne_sans_course_et_sans_signal_est_deconnecte(reprise):
    s = reprise
    id1, l1 = await s.utilisateur(Role.LIVREUR, "Koffi")
    await s.connecter(l1, pt(1, 0))
    assert await s.plateforme.superviseur.passe() == []  # signal récent : il reste en ligne
    agent = s.plateforme.agent_livreur(str(id1))
    agent.situation = dataclasses.replace(
        agent.situation, position_le=datetime.now(UTC) - timedelta(seconds=ReglagesSupervision().deconnexion_apres_s + 5))
    assert await s.plateforme.superviseur.passe() == [(str(id1), "deconnexion")]
    assert s.plateforme.agent_livreur(str(id1)) is None
    async with s.fabrique() as session:
        assert (await session.get(Livreur, id1)).en_ligne is False
        assert len((await session.scalars(select(Position))).all()) == 1  # la connexion a enregistré sa position
