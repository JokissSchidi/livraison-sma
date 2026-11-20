"""Tests du Dispatcher : Contract Net complet sur un VRAI serveur XMPP (voir test_agents.py)."""
import asyncio
import dataclasses

import pytest
import pytest_asyncio
from outils import FauxMoteur, pt
from test_agents import comptes, situation  # noqa: F401  (fixture « comptes » et fabrique de situation)

from livraison.agents.annuaire import AnnuaireAgent
from livraison.agents.dispatcher import DispatcherAgent, ReglagesDispatcher, Statut
from livraison.agents.gestionnaire import GestionnaireLivreurs
from livraison.config import parametres
from livraison.modele.offre import Coefficients, Demande

COLIS = Demande(enlevement=pt(0, 0), livraison=pt(0, 5), poids_kg=2.0)
REGL = ReglagesDispatcher(rayon_initial_m=8_000, delai_offres_s=2.0, delai_acceptation_s=3.0,
                          delai_confirmation_s=1.0, delai_relance_s=0.6, max_tentatives=3)


class Banc:
    def __init__(self, annuaire, gestionnaire, comptes_):
        self.annuaire, self.gestionnaire, self.comptes = annuaire, gestionnaire, comptes_
        self.dispatchers: list[DispatcherAgent] = []

    async def livreur(self, lid, position, **kw):
        agent = await self.gestionnaire.connecter(situation(lid, position, **kw))
        await attendre(lambda: lid in self.annuaire.annuaire._entrees)  # enregistré à l'annuaire
        return agent

    async def dispatcher(self, reglages=REGL, **kw) -> DispatcherAgent:
        await self.comptes.assurer("dispatcher-test")
        d = DispatcherAgent(f"dispatcher-test@{parametres.xmpp_domaine}", parametres.xmpp_mot_de_passe_agents,
                            f"annuaire-test@{parametres.xmpp_domaine}", reglages, port=parametres.xmpp_port, **kw)
        await d.start(auto_register=False)
        self.dispatchers.append(d)
        return d

    def etat(self, lid):
        return self.gestionnaire.agent(lid).situation


async def attendre(condition, delai=8.0):
    fin = asyncio.get_running_loop().time() + delai
    while not condition():
        assert asyncio.get_running_loop().time() < fin, "condition jamais remplie"
        await asyncio.sleep(0.05)


@pytest_asyncio.fixture
async def banc(comptes):  # noqa: F811
    domaine, mdp = parametres.xmpp_domaine, parametres.xmpp_mot_de_passe_agents
    await comptes.assurer("annuaire-test")
    annuaire = AnnuaireAgent(f"annuaire-test@{domaine}", mdp, port=parametres.xmpp_port)
    await annuaire.start(auto_register=False)
    gestionnaire = GestionnaireLivreurs(f"annuaire-test@{domaine}", FauxMoteur(), Coefficients(), comptes)
    banc = Banc(annuaire, gestionnaire, comptes)
    try:
        yield banc
    finally:
        for d in banc.dispatchers:
            await d.stop()
        await gestionnaire.tout_arreter()
        await annuaire.stop()


# ----------------------------------------------------------------------------- attribution simple

async def test_la_commande_va_au_livreur_le_plus_proche(banc):
    for lid, x in (("A", 1), ("B", 3), ("C", 6)):
        await banc.livreur(lid, pt(x, 0))
    evenements = []
    d = await banc.dispatcher(sur_evenement=lambda c, t, det: evenements.append(t))
    r = await d.soumettre("cmd-1", COLIS)
    assert r.statut == Statut.ATTRIBUEE and r.attribution.livreur_id == "A"
    assert r.attribution.proposition.cout == pytest.approx(100, rel=0.02)  # 1 km à 36 km/h
    assert [banc.etat(x).nb_courses for x in "ABC"] == [1, 0, 0]
    ordre = [e for e in evenements if e in ("soumise", "cfp_envoye", "accept_envoye", "attribuee")]
    assert ordre == ["soumise", "cfp_envoye", "accept_envoye", "attribuee"]


async def test_la_charge_du_livreur_est_prise_en_compte(banc):
    # A est le plus proche mais déjà chargé de deux courses ailleurs : B, libre, est meilleur.
    await banc.livreur("A", pt(1, 0), nb_courses=2, charge_kg=4, arrets_restants=(pt(1, -10), pt(1, -12)))
    await banc.livreur("B", pt(3, 0))
    d = await banc.dispatcher()
    r = await d.soumettre("cmd-1", COLIS)
    assert r.attribution.livreur_id == "B"


async def test_une_offre_tardive_est_ignoree(banc):
    class Lent(FauxMoteur):
        async def matrice(self, *a, **k):
            await asyncio.sleep(2.5)
            return await super().matrice(*a, **k)

    lent = await banc.livreur("A", pt(1, 0))
    lent.itineraires = Lent()  # répond après la fin de la collecte
    await banc.livreur("B", pt(3, 0))
    d = await banc.dispatcher(dataclasses.replace(REGL, delai_offres_s=1.0))
    r = await d.soumettre("cmd-1", COLIS)
    assert r.attribution.livreur_id == "B"
    await asyncio.sleep(2.5)  # la réponse tardive de A arrive : sans effet
    assert banc.etat("A").nb_courses == 0 and d.suivis["cmd-1"].attribution.livreur_id == "B"


# ----------------------------------------------------------------------------- confirmation humaine

async def test_refus_du_livreur_propose_au_suivant_et_libere_sa_place(banc):
    await banc.livreur("A", pt(1, 0))
    await banc.livreur("B", pt(3, 0))
    refus = []

    async def confirmer(livreur_id, *_):
        if livreur_id == "A":
            refus.append(livreur_id)
            return False
        return True

    d = await banc.dispatcher(confirmer=confirmer)
    r = await d.soumettre("cmd-1", COLIS)
    assert r.attribution.livreur_id == "B" and refus == ["A"]
    await attendre(lambda: banc.etat("A").nb_courses == 0)  # la course réservée a été libérée
    assert banc.etat("B").nb_courses == 1


async def test_silence_du_livreur_propose_au_suivant(banc):
    await banc.livreur("A", pt(1, 0))
    await banc.livreur("B", pt(3, 0))

    async def confirmer(livreur_id, *_):
        if livreur_id == "A":
            await asyncio.sleep(10)  # ne répond pas dans le délai (1 s dans ces tests)
        return True

    d = await banc.dispatcher(confirmer=confirmer)
    r = await d.soumettre("cmd-1", COLIS)
    assert r.attribution.livreur_id == "B"
    await attendre(lambda: banc.etat("A").nb_courses == 0)


# ----------------------------------------------------------------------------- file d'attente (F-33)

async def test_commande_sans_livreur_attribuee_des_qu_un_livreur_se_connecte(banc):
    d = await banc.dispatcher(dataclasses.replace(REGL, delai_relance_s=30))
    r = await d.soumettre("cmd-1", COLIS)
    assert r.statut == Statut.EN_FILE and d.en_file() == ["cmd-1"]
    await banc.livreur("A", pt(1, 0))
    d.reveiller()
    final = await d.attendre("cmd-1", 10)
    assert final.statut == Statut.ATTRIBUEE and final.attribution.livreur_id == "A"
    assert final.attribution.tentative == 1  # un réveil n'use pas une tentative


async def test_le_rayon_s_elargit_a_chaque_relance(banc):
    await banc.livreur("A", pt(5, 0))  # à 5 km : hors du rayon initial de 3 km, dans celui de 6 km
    d = await banc.dispatcher(dataclasses.replace(REGL, rayon_initial_m=3_000, delai_relance_s=0.5))
    r = await d.soumettre("cmd-1", COLIS)
    assert r.statut == Statut.EN_FILE
    final = await d.attendre("cmd-1", 10)
    assert final.statut == Statut.ATTRIBUEE
    assert final.attribution.tentative == 2 and d.suivis["cmd-1"].rayon_m == 6_000


async def test_apres_le_maximum_de_tentatives_le_repartiteur_est_alerte(banc):
    alertes = []
    d = await banc.dispatcher(dataclasses.replace(REGL, max_tentatives=2, delai_relance_s=0.4),
                              signaler=lambda c, raison: alertes.append((c, raison)))
    await d.soumettre("cmd-1", COLIS)
    final = await d.attendre("cmd-1", 10)
    assert final.statut == Statut.ECHEC
    assert alertes and alertes[0][0] == "cmd-1"


async def test_les_commandes_urgentes_passent_en_premier(banc):
    banc.gestionnaire.coef = Coefficients(max_courses=1)
    d = await banc.dispatcher(dataclasses.replace(REGL, delai_relance_s=30))
    await d.soumettre("normale", COLIS)
    await d.soumettre("urgente", COLIS, urgente=True)  # arrivée après, mais prioritaire
    assert d.en_file() == ["urgente", "normale"]
    await banc.livreur("A", pt(1, 0))  # un seul livreur disponible
    d.reveiller()
    final = await d.attendre("urgente", 10)
    assert final.statut == Statut.ATTRIBUEE and final.attribution.livreur_id == "A"
    # La commande normale tente ensuite sa chance, trouve le livreur déjà pris et retourne en file.
    await attendre(lambda: d.suivis["normale"].statut == Statut.EN_FILE)
    assert d.suivis["normale"].attribution is None


# ----------------------------------------------------------------------------- simultanéité (F-32)

async def test_cinq_commandes_simultanees_trois_livreurs_sans_double_attribution(banc):
    banc.gestionnaire.coef = Coefficients(max_courses=1)  # une course à la fois par livreur
    for lid, x in (("A", 1), ("B", 2), ("C", 3)):
        await banc.livreur(lid, pt(x, 0))
    d = await banc.dispatcher(dataclasses.replace(REGL, delai_relance_s=30))
    resultats = await asyncio.gather(*(d.soumettre(f"cmd-{i}", COLIS) for i in range(5)))
    attribuees = [r for r in resultats if r.statut == Statut.ATTRIBUEE]
    assert len(attribuees) == 3
    assert len({r.attribution.livreur_id for r in attribuees}) == 3  # jamais deux commandes pour un même livreur
    assert sorted(r.statut for r in resultats).count(Statut.EN_FILE) == 2
    assert all(banc.etat(x).nb_courses == 1 for x in "ABC")

    # Deux livreurs se connectent : les deux commandes en attente sont attribuées automatiquement.
    await banc.livreur("D", pt(4, 0))
    await banc.livreur("E", pt(5, 0))
    d.reveiller()
    en_attente = d.en_file()
    finales = await asyncio.gather(*(d.attendre(c, 15) for c in en_attente))
    assert all(f.statut == Statut.ATTRIBUEE for f in finales)
    tous = {s.attribution.livreur_id for s in d.suivis.values()}
    assert tous == set("ABCDE")


# ----------------------------------------------------------------------------- réattribution (F-34)

async def test_reattribution_apres_panne_du_livreur(banc):
    await banc.livreur("A", pt(1, 0))
    await banc.livreur("B", pt(3, 0))
    d = await banc.dispatcher()
    assert (await d.soumettre("cmd-1", COLIS)).attribution.livreur_id == "A"
    r = await d.reattribuer("cmd-1", "perte du signal GPS")
    assert r.statut == Statut.ATTRIBUEE and r.attribution.livreur_id == "B"
    await attendre(lambda: banc.etat("A").nb_courses == 0)  # A est déchargé de la course
    assert banc.etat("B").nb_courses == 1 and "A" in d.suivis["cmd-1"].exclus


async def test_annulation_libere_le_livreur(banc):
    await banc.livreur("A", pt(1, 0))
    d = await banc.dispatcher()
    await d.soumettre("cmd-1", COLIS)
    await d.annuler("cmd-1")
    assert d.suivis["cmd-1"].statut == Statut.ANNULEE
    await attendre(lambda: banc.etat("A").nb_courses == 0 and not banc.etat("A").arrets_restants)
