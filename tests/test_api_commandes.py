"""Parcours complet par l'API : client, livreur, attribution, base de données.

Nécessite PostgreSQL (base de test) et le serveur XMPP (voir conftest.py et test_agents.py).
"""
import asyncio
import dataclasses
import uuid

import pytest
import pytest_asyncio
from conftest import URL_BASE_TEST
from httpx import ASGITransport, AsyncClient
from outils import FauxMoteur, pt
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from test_agents import comptes  # noqa: F401  (fixture)

from livraison.agents.dispatcher import ReglagesDispatcher
from livraison.agents.plateforme import Plateforme
from livraison.api.main import app
from livraison.config import parametres
from livraison.db.base import obtenir_session
from livraison.db.modeles import (
    Commande,
    Livreur,
    Offre,
    Position,
    ResultatOffre,
    Role,
    Utilisateur,
    ValidationLivreur,
    Vehicule,
)
from livraison.services.securite import creer_jeton

REGL = ReglagesDispatcher(rayon_initial_m=8_000, delai_offres_s=2.0, delai_acceptation_s=3.0,
                          delai_confirmation_s=6.0, delai_relance_s=30.0, max_tentatives=3)
COMMANDE = {
    "adresse_enlevement": "Plateau, Abidjan", "enlevement": {"latitude": pt(0, 0).latitude, "longitude": pt(0, 0).longitude},
    "adresse_livraison": "Cocody, Abidjan", "livraison": {"latitude": pt(0, 5).latitude, "longitude": pt(0, 5).longitude},
    "description_colis": "Documents", "poids_kg": 2.0,
}
TABLES = "evenements, offres, paiements, factures, positions, commandes, livreurs, codes_verification, utilisateurs"


def gps(c):
    return {"latitude": c.latitude, "longitude": c.longitude}


class Systeme:
    def __init__(self, api, fabrique, plateforme):
        self.api, self.fabrique, self.plateforme = api, fabrique, plateforme
        self._n = 0

    async def utilisateur(self, role: Role, nom: str, *, validation=None, vehicule=Vehicule.MOTO):
        """Crée un compte directement en base et renvoie (id, en-tête d'authentification)."""
        self._n += 1
        async with self.fabrique() as s:
            u = Utilisateur(telephone=f"+22501{self._n:08d}", nom=nom, role=role)
            s.add(u)
            await s.flush()
            if role == Role.LIVREUR:
                s.add(Livreur(utilisateur_id=u.id, vehicule=vehicule, capacite_kg=30,
                              validation=validation or ValidationLivreur.VALIDE))
            await s.commit()
            return u.id, {"Authorization": f"Bearer {creer_jeton(u.id, role.value)}"}

    async def attendre(self, appel, condition, delai=12.0):
        """Rappelle `appel()` (une coroutine renvoyant une réponse JSON) jusqu'à ce que condition(json) soit vraie."""
        fin = asyncio.get_running_loop().time() + delai
        while True:
            reponse = await appel()
            if condition(reponse):
                return reponse
            assert asyncio.get_running_loop().time() < fin, f"condition jamais remplie ; dernière réponse : {reponse}"
            await asyncio.sleep(0.1)

    async def propositions(self, entetes):
        return (await self.api.get("/livreurs/moi/propositions", headers=entetes)).json()

    async def commande(self, cid, entetes):
        r = await self.api.get(f"/commandes/{cid}", headers=entetes)
        return r.json() if r.status_code == 200 else {"http": r.status_code}

    async def connecter(self, entetes, position):
        r = await self.api.post("/livreurs/moi/connexion", json=gps(position), headers=entetes)
        assert r.status_code == 200, r.text


@pytest_asyncio.fixture
async def systeme(base_de_test, comptes, monkeypatch):  # noqa: F811
    monkeypatch.setattr(parametres, "jwt_secret", "x" * 40)
    moteur = create_async_engine(URL_BASE_TEST, poolclass=NullPool)
    fabrique = async_sessionmaker(moteur, expire_on_commit=False)
    async with moteur.begin() as cx:
        await cx.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    plateforme = Plateforme(fabrique, comptes=comptes, itineraires=FauxMoteur(), reglages=REGL, prefixe_jid="t5-")
    await plateforme.demarrer()
    app.state.plateforme = plateforme

    async def session_de_requete():
        async with fabrique() as s:
            yield s

    app.dependency_overrides[obtenir_session] = session_de_requete
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as api:
            yield Systeme(api, fabrique, plateforme)
    finally:
        app.dependency_overrides.clear()
        app.state.plateforme = None
        await plateforme.arreter()
        async with moteur.begin() as cx:
            await cx.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
        await moteur.dispose()


# ----------------------------------------------------------------------------- parcours nominal

async def test_parcours_complet_client_livreur_attribution(systeme):
    s = systeme
    _, client = await s.utilisateur(Role.CLIENT, "Awa")
    livreur_id, livreur = await s.utilisateur(Role.LIVREUR, "Koffi")
    _, admin = await s.utilisateur(Role.ADMINISTRATEUR, "Admin")

    await s.connecter(livreur, pt(1, 0))
    r = await s.api.post("/commandes", json=COMMANDE, headers=client)
    assert r.status_code == 201 and r.json()["etat"] == "creee"
    cid = r.json()["id"]

    # Le livreur voit la course proposée, avec le délai pour répondre.
    props = await s.attendre(lambda: s.propositions(livreur), lambda p: len(p) == 1)
    assert props[0]["commande_id"] == cid and props[0]["adresse_livraison"] == "Cocody, Abidjan"
    assert 0 < props[0]["secondes_restantes"] <= 6 and props[0]["duree_approche_s"] == pytest.approx(100, rel=0.05)
    assert (await s.api.post(f"/livreurs/moi/propositions/{cid}/accepter", headers=livreur)).json() == {"reponse": "acceptee"}

    final = await s.attendre(lambda: s.commande(cid, client), lambda c: c.get("etat") == "attribuee")
    assert final["livreur_id"] == str(livreur_id) and final["attribuee_le"] is not None
    assert (await s.propositions(livreur)) == []  # plus rien en attente

    # Base de données : offre retenue et journal des événements.
    await s.plateforme.depot.vider()
    async with s.fabrique() as session:
        offres = (await session.scalars(select(Offre))).all()
    assert len(offres) == 1 and offres[0].resultat == ResultatOffre.RETENUE and float(offres[0].cout) == pytest.approx(100, rel=0.05)
    detail = await s.commande(cid, admin)  # le répartiteur voit le journal complet
    types = [e["type"] for e in detail["evenements"]]
    assert [t for t in types if t in ("soumise", "cfp_envoye", "offre_recue", "accept_envoye", "attribuee")] == \
        ["soumise", "cfp_envoye", "offre_recue", "accept_envoye", "attribuee"]
    assert "evenements" not in (await s.commande(cid, client)) or (await s.commande(cid, client))["evenements"] is None


async def test_le_livreur_decline_la_course_va_au_suivant(systeme):
    s = systeme
    _, client = await s.utilisateur(Role.CLIENT, "Awa")
    id1, l1 = await s.utilisateur(Role.LIVREUR, "Premier")
    id2, l2 = await s.utilisateur(Role.LIVREUR, "Second")
    await s.connecter(l1, pt(1, 0))
    cid = (await s.api.post("/commandes", json=COMMANDE, headers=client)).json()["id"]
    await s.attendre(lambda: s.propositions(l1), lambda p: len(p) == 1)
    assert (await s.api.post(f"/livreurs/moi/propositions/{cid}/decliner", headers=l1)).json() == {"reponse": "declinee"}

    # Personne d'autre : la commande attend en file d'attente...
    en_attente = await s.attendre(lambda: s.commande(cid, client), lambda c: c.get("etat") == "en_recherche")
    assert en_attente["livreur_id"] is None
    # ...puis un second livreur se connecte : elle lui est proposée automatiquement.
    await s.connecter(l2, pt(3, 0))
    await s.attendre(lambda: s.propositions(l2), lambda p: len(p) == 1)
    assert (await s.propositions(l1)) == []  # celui qui a décliné ne la revoit pas
    await s.api.post(f"/livreurs/moi/propositions/{cid}/accepter", headers=l2)
    final = await s.attendre(lambda: s.commande(cid, client), lambda c: c.get("etat") == "attribuee")
    assert final["livreur_id"] == str(id2)
    await s.plateforme.depot.vider()
    async with s.fabrique() as session:
        resultats = {str(o.livreur_id): o.resultat for o in (await session.scalars(select(Offre))).all()}
    assert resultats[str(id1)] == ResultatOffre.REFUSEE and resultats[str(id2)] == ResultatOffre.RETENUE


async def test_proposition_sans_reponse_expire(systeme):
    s = systeme
    s.plateforme.dispatcher.reglages = dataclasses.replace(REGL, delai_confirmation_s=1.0)
    _, client = await s.utilisateur(Role.CLIENT, "Awa")
    _, livreur = await s.utilisateur(Role.LIVREUR, "Silencieux")
    await s.connecter(livreur, pt(1, 0))
    cid = (await s.api.post("/commandes", json=COMMANDE, headers=client)).json()["id"]
    await s.attendre(lambda: s.propositions(livreur), lambda p: len(p) == 1)
    await s.attendre(lambda: s.propositions(livreur), lambda p: len(p) == 0)  # délai dépassé
    r = await s.api.post(f"/livreurs/moi/propositions/{cid}/accepter", headers=livreur)
    assert r.status_code == 404
    assert (await s.attendre(lambda: s.commande(cid, client), lambda c: c.get("etat") == "en_recherche"))["livreur_id"] is None


# ----------------------------------------------------------------------------- règles d'accès

async def test_un_livreur_non_valide_ne_peut_pas_se_connecter(systeme):
    s = systeme
    _, livreur = await s.utilisateur(Role.LIVREUR, "En attente", validation=ValidationLivreur.EN_ATTENTE)
    r = await s.api.post("/livreurs/moi/connexion", json=gps(pt(0, 0)), headers=livreur)
    assert r.status_code == 403 and "validé" in r.json()["detail"]


async def test_roles_et_authentification(systeme):
    s = systeme
    _, client = await s.utilisateur(Role.CLIENT, "Awa")
    _, livreur = await s.utilisateur(Role.LIVREUR, "Koffi")
    assert (await s.api.post("/commandes", json=COMMANDE)).status_code == 401
    assert (await s.api.post("/commandes", json=COMMANDE, headers=livreur)).status_code == 403  # un livreur ne commande pas
    assert (await s.api.post("/livreurs/moi/connexion", json=gps(pt(0, 0)), headers=client)).status_code == 403
    assert (await s.api.get("/livreurs/moi/propositions")).status_code == 401


async def test_un_client_ne_voit_pas_les_commandes_d_un_autre(systeme):
    s = systeme
    _, awa = await s.utilisateur(Role.CLIENT, "Awa")
    _, bintou = await s.utilisateur(Role.CLIENT, "Bintou")
    _, repartiteur = await s.utilisateur(Role.REPARTITEUR, "Superviseur")
    cid = (await s.api.post("/commandes", json=COMMANDE, headers=awa)).json()["id"]
    assert (await s.api.get(f"/commandes/{cid}", headers=bintou)).status_code == 404
    assert (await s.api.get("/commandes", headers=bintou)).json() == []
    assert [c["id"] for c in (await s.api.get("/commandes", headers=awa)).json()] == [cid]
    assert (await s.api.get(f"/commandes/{cid}", headers=repartiteur)).status_code == 200  # le personnel voit tout
    assert (await s.api.post(f"/commandes/{cid}/annuler", headers=bintou)).status_code == 404


async def test_donnees_invalides_refusees(systeme):
    s = systeme
    _, client = await s.utilisateur(Role.CLIENT, "Awa")
    for changement in ({"poids_kg": 0}, {"poids_kg": 9999}, {"adresse_livraison": "x"},
                       {"enlevement": {"latitude": 200, "longitude": 0}}):
        r = await s.api.post("/commandes", json={**COMMANDE, **changement}, headers=client)
        assert r.status_code == 422, changement


# ----------------------------------------------------------------------------- position, annulation

async def test_position_gps_enregistree_et_mise_a_jour(systeme):
    s = systeme
    livreur_id, livreur = await s.utilisateur(Role.LIVREUR, "Koffi")
    hors = await s.api.post("/livreurs/moi/position", json=gps(pt(0, 0)), headers=livreur)
    assert hors.status_code == 409  # pas connecté
    await s.connecter(livreur, pt(1, 0))
    r = await s.api.post("/livreurs/moi/position", json={**gps(pt(25, 0)), "precision_m": 8.5}, headers=livreur)
    assert r.status_code == 200
    async with s.fabrique() as session:
        positions = (await session.scalars(select(Position))).all()
    assert len(positions) == 1 and float(positions[0].precision_m) == 8.5
    assert s.plateforme.agent_livreur(str(livreur_id)).situation.position == pt(25, 0)
    assert (await s.api.post("/livreurs/moi/deconnexion", headers=livreur)).json() == {"etat": "hors_ligne"}
    assert s.plateforme.agent_livreur(str(livreur_id)) is None


async def test_annulation_par_le_client(systeme):
    s = systeme
    _, client = await s.utilisateur(Role.CLIENT, "Awa")
    cid = (await s.api.post("/commandes", json=COMMANDE, headers=client)).json()["id"]  # aucun livreur : en recherche
    await s.attendre(lambda: s.commande(cid, client), lambda c: c.get("etat") == "en_recherche")
    r = await s.api.post(f"/commandes/{cid}/annuler", headers=client)
    assert r.status_code == 200 and r.json()["etat"] == "annulee"
    assert (await s.api.post(f"/commandes/{cid}/annuler", headers=client)).status_code == 409  # déjà annulée
    async with s.fabrique() as session:
        assert (await session.get(Commande, uuid.UUID(cid))).etat.value == "annulee"


async def test_503_si_la_plateforme_n_est_pas_disponible(systeme):
    s = systeme
    _, client = await s.utilisateur(Role.CLIENT, "Awa")
    app.state.plateforme = None
    r = await s.api.post("/commandes", json=COMMANDE, headers=client)
    assert r.status_code == 503
    app.state.plateforme = s.plateforme
