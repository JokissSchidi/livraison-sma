"""Tests des agents Annuaire et Livreur sur un VRAI serveur XMPP.

Prérequis : `docker compose up -d` (service xmpp). Les tests s'ignorent si le serveur
ou la commande de création de comptes (XMPP_CTL) est indisponible.
"""
import asyncio
import json
import socket
import uuid

import pytest
import pytest_asyncio
from outils import FauxMoteur, pt
from spade.agent import Agent
from spade.behaviour import OneShotBehaviour
from spade.message import Message

from livraison.agents import messages as m
from livraison.agents.annuaire import AnnuaireAgent
from livraison.agents.gestionnaire import GestionnaireLivreurs, jid_livreur
from livraison.config import parametres
from livraison.db.modeles import Vehicule
from livraison.modele.offre import Coefficients, Demande, MotifRefus, SituationLivreur
from livraison.services.comptes_xmpp import ComptesEjabberd, ErreurCompteXMPP

COLIS = Demande(enlevement=pt(5, 0), livraison=pt(5, 5), poids_kg=2.0)


@pytest.fixture(scope="session")
def comptes():
    try:
        with socket.create_connection((parametres.xmpp_host, parametres.xmpp_port), timeout=3):
            pass
    except OSError:
        pytest.skip("Serveur XMPP injoignable : lancez `docker compose up -d`")
    c = ComptesEjabberd()
    try:
        asyncio.run(c.assurer("sonde-tests"))
    except ErreurCompteXMPP as e:
        pytest.skip(f"Création de comptes XMPP impossible (réglage XMPP_CTL) : {e}")
    return c


class ClientTest(Agent):
    async def setup(self):
        pass

    async def echanger(self, msg: Message, timeout: float = 10) -> Message | None:
        """Envoie un message et attend UNE réponse."""
        resultat = {}

        class Echange(OneShotBehaviour):
            async def run(self):
                await self.send(msg)
                resultat["reponse"] = await self.receive(timeout=timeout)

        b = Echange()
        self.add_behaviour(b)
        await b.join()
        return resultat.get("reponse")


class Plateforme:
    def __init__(self, annuaire, clients, gestionnaire):
        self.annuaire, self.clients, self.gestionnaire = annuaire, clients, gestionnaire

    @property
    def client(self):
        return self.clients[0]

    async def chercher(self, centre, rayon_m, vehicules=None) -> list[dict]:
        corps = {"action": "chercher", "centre": m.coord_vers_json(centre), "rayon_m": rayon_m}
        if vehicules:
            corps["vehicules"] = vehicules
        rep = await self.client.echanger(m.creer(str(self.annuaire.jid), m.REQUEST, corps))
        assert rep is not None, "L'annuaire n'a pas répondu"
        return m.lire(rep)["livreurs"]

    async def attendre_annuaire(self, condition, centre=pt(0, 0), rayon_m=50_000, delai=8.0):
        """Interroge l'annuaire jusqu'à ce que condition(liste_des_ids) soit vraie."""
        fin = asyncio.get_running_loop().time() + delai
        while True:
            ids = [x["livreur_id"] for x in await self.chercher(centre, rayon_m)]
            if condition(ids) or asyncio.get_running_loop().time() > fin:
                return ids
            await asyncio.sleep(0.3)


@pytest_asyncio.fixture
async def plateforme(comptes):
    domaine, mdp = parametres.xmpp_domaine, parametres.xmpp_mot_de_passe_agents
    for nom in ("annuaire-test", "client-test-1", "client-test-2"):
        await comptes.assurer(nom)
    demarres: list[Agent] = []
    gestionnaire = None
    try:
        annuaire = AnnuaireAgent(f"annuaire-test@{domaine}", mdp, port=parametres.xmpp_port)
        await annuaire.start(auto_register=False)
        demarres.append(annuaire)
        clients = []
        for i in (1, 2):
            c = ClientTest(f"client-test-{i}@{domaine}", mdp, port=parametres.xmpp_port)
            await c.start(auto_register=False)
            demarres.append(c)
            clients.append(c)
        gestionnaire = GestionnaireLivreurs(f"annuaire-test@{domaine}", FauxMoteur(), Coefficients(), comptes)
        yield Plateforme(annuaire, clients, gestionnaire)
    finally:  # les agents sont toujours arrêtés, même si le test ou le démarrage échoue
        if gestionnaire is not None:
            await gestionnaire.tout_arreter()
        for agent in reversed(demarres):
            await agent.stop()


def situation(lid=None, position=None, **kw) -> SituationLivreur:
    from datetime import UTC, datetime

    return SituationLivreur(**{**dict(
        livreur_id=lid or uuid.uuid4().hex[:12], vehicule=Vehicule.MOTO, capacite_kg=30.0,
        position=position or pt(0, 0), position_le=datetime.now(UTC)), **kw})


def cfp(vers, demande=COLIS, commande=None):
    return m.creer(vers, m.CFP, {"demande": m.demande_vers_json(demande)}, thread=commande or uuid.uuid4().hex)


def accepter(vers, commande, demande=COLIS):
    return m.creer(vers, m.ACCEPT, {"demande": m.demande_vers_json(demande)}, thread=commande)


# ----------------------------------------------------------------------------- annuaire

async def test_livreurs_en_ligne_apparaissent_dans_l_annuaire(plateforme):
    g = plateforme.gestionnaire
    await g.connecter(situation("proche", pt(1, 0)))
    await g.connecter(situation("moyen", pt(4, 0), vehicule=Vehicule.VOITURE))
    ids = await plateforme.attendre_annuaire(lambda i: len(i) >= 2)
    assert ids == ["proche", "moyen"]  # triés par distance au point cherché
    assert [x["livreur_id"] for x in await plateforme.chercher(pt(0, 0), 3_000)] == ["proche"]
    assert [x["livreur_id"] for x in await plateforme.chercher(pt(0, 0), 50_000, ["voiture"])] == ["moyen"]


async def test_deconnexion_retire_le_livreur_de_l_annuaire(plateforme):
    g = plateforme.gestionnaire
    await g.connecter(situation("temporaire", pt(1, 0)))
    assert "temporaire" in await plateforme.attendre_annuaire(lambda i: "temporaire" in i)
    assert await g.deconnecter("temporaire") is True
    assert "temporaire" not in await plateforme.attendre_annuaire(lambda i: "temporaire" not in i)
    assert len(g) == 0 and await g.deconnecter("temporaire") is False


async def test_connecter_deux_fois_ne_cree_qu_un_agent(plateforme):
    g = plateforme.gestionnaire
    a1 = await g.connecter(situation("unique"))
    a2 = await g.connecter(situation("unique"))
    assert a1 is a2 and len(g) == 1


async def test_la_position_gps_met_a_jour_l_annuaire(plateforme):
    agent = await plateforme.gestionnaire.connecter(situation("mobile", pt(0, 0)))
    await plateforme.attendre_annuaire(lambda i: "mobile" in i)
    await agent.maj_position(pt(30, 0))
    ids = await plateforme.attendre_annuaire(lambda i: "mobile" not in i, centre=pt(0, 0), rayon_m=5_000)
    assert "mobile" not in ids
    assert "mobile" in await plateforme.attendre_annuaire(lambda i: "mobile" in i, centre=pt(30, 0), rayon_m=1_000)


async def test_message_invalide_recoit_un_echec(plateforme):
    rep = await plateforme.client.echanger(m.creer(str(plateforme.annuaire.jid), m.REQUEST, {"action": "danser"}))
    assert m.performative(rep) == m.FAILURE


# ----------------------------------------------------------------------------- Contract Net côté livreur

async def test_cfp_recoit_une_proposition(plateforme):
    await plateforme.gestionnaire.connecter(situation("p1"))
    commande = uuid.uuid4().hex
    rep = await plateforme.client.echanger(cfp(jid_livreur("p1"), commande=commande))
    assert m.performative(rep) == m.PROPOSE
    assert rep.thread == commande  # même identifiant de conversation
    p = m.proposition_depuis_json(m.lire(rep))
    assert p.livreur_id == "p1" and p.cout == pytest.approx(500, rel=0.02)


async def test_cfp_trop_lourd_recoit_un_refus_motive(plateforme):
    await plateforme.gestionnaire.connecter(situation("p2"))
    lourd = Demande(COLIS.enlevement, COLIS.livraison, 500.0)
    rep = await plateforme.client.echanger(cfp(jid_livreur("p2"), lourd))
    assert m.performative(rep) == m.REFUSE
    assert m.refus_depuis_json(m.lire(rep)).motif == MotifRefus.TROP_LOURD


async def test_cfp_mal_forme_recoit_un_echec(plateforme):
    await plateforme.gestionnaire.connecter(situation("p3"))
    msg = m.creer(jid_livreur("p3"), m.CFP, {"demande": {"poids_kg": "beaucoup"}}, thread="x")
    assert m.performative(await plateforme.client.echanger(msg)) == m.FAILURE


async def test_acceptation_puis_saturation(plateforme):
    plateforme.gestionnaire.coef = Coefficients(max_courses=2)
    agent = await plateforme.gestionnaire.connecter(situation("s1"))
    cible = jid_livreur("s1")
    for i in range(2):
        rep = await plateforme.client.echanger(accepter(cible, f"cmd-{i}"))
        assert m.performative(rep) == m.INFORM
    assert agent.situation.nb_courses == 2 and len(agent.situation.arrets_restants) == 4
    rep = await plateforme.client.echanger(accepter(cible, "cmd-3"))
    assert m.performative(rep) == m.FAILURE
    assert m.refus_depuis_json(m.lire(rep)).motif == MotifRefus.SATURE
    assert agent.situation.nb_courses == 2


async def test_acceptation_recue_deux_fois_n_est_comptee_qu_une_fois(plateforme):
    agent = await plateforme.gestionnaire.connecter(situation("d1"))
    for _ in range(2):
        rep = await plateforme.client.echanger(accepter(jid_livreur("d1"), "meme-commande"))
        assert m.performative(rep) == m.INFORM
    assert agent.situation.nb_courses == 1


async def test_deux_acceptations_simultanees_une_seule_passe(plateforme):
    """F-32 : un livreur limité à 1 course ne peut pas être attribué deux fois en même temps."""
    plateforme.gestionnaire.coef = Coefficients(max_courses=1)
    await plateforme.gestionnaire.connecter(situation("c1"))
    cible = jid_livreur("c1")
    reponses = await asyncio.gather(
        plateforme.clients[0].echanger(accepter(cible, "cmd-A")),
        plateforme.clients[1].echanger(accepter(cible, "cmd-B")),
    )
    assert sorted(m.performative(r) for r in reponses) == [m.FAILURE, m.INFORM]
