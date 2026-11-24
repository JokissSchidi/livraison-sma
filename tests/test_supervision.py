"""Surveillance du signal GPS : alerte puis réattribution (F-34). Nécessite PostgreSQL et XMPP."""
import dataclasses
from datetime import UTC, datetime, timedelta

from outils import pt
from sqlalchemy import update
from test_agents import comptes  # noqa: F401  (fixture)
from test_api_commandes import COMMANDE, gps, systeme  # noqa: F401  (fixture « systeme » et données communes)

from livraison.db.modeles import Commande, EtatCommande, Role


def silence(systeme, livreur_id, secondes):
    """Simule un livreur muet depuis `secondes` : sa dernière position date de ce moment-là."""
    agent = systeme.plateforme.agent_livreur(str(livreur_id))
    agent.situation = dataclasses.replace(agent.situation, position_le=datetime.now(UTC) - timedelta(seconds=secondes))


async def commande_attribuee(s, client, livreur, position):
    await s.connecter(livreur, position)
    cid = (await s.api.post("/commandes", json=COMMANDE, headers=client)).json()["id"]
    await s.attendre(lambda: s.propositions(livreur), lambda p: len(p) == 1)
    await s.api.post(f"/livreurs/moi/propositions/{cid}/accepter", headers=livreur)
    await s.attendre(lambda: s.commande(cid, client), lambda c: c.get("etat") == "attribuee")
    return cid


async def test_signal_perdu_alerte_puis_reattribution(systeme):
    s = systeme
    _, client = await s.utilisateur(Role.CLIENT, "Awa")
    id1, l1 = await s.utilisateur(Role.LIVREUR, "Premier")
    id2, l2 = await s.utilisateur(Role.LIVREUR, "Second")
    cid = await commande_attribuee(s, client, l1, pt(1, 0))
    await s.connecter(l2, pt(3, 0))
    sup = s.plateforme.superviseur

    assert await sup.passe() == []  # tout va bien : le livreur envoie sa position

    silence(s, id1, 60)  # plus de nouvelles depuis 60 s : alerte, mais pas encore de réattribution
    assert await sup.passe() == [(cid, "alerte")]
    assert s.plateforme.alertes[-1][0] == cid and "perdu" in s.plateforme.alertes[-1][1]
    assert await sup.passe() == []  # une seule alerte par épisode de silence
    assert (await s.commande(cid, client))["etat"] == "attribuee"

    silence(s, id1, 150)  # silence prolongé : la commande est reprise et proposée au livreur suivant
    assert await sup.passe() == [(cid, "reattribution")]
    props = await s.attendre(lambda: s.propositions(l2), lambda p: len(p) == 1)
    assert props[0]["commande_id"] == cid
    await s.api.post(f"/livreurs/moi/propositions/{cid}/accepter", headers=l2)
    final = await s.attendre(lambda: s.commande(cid, client), lambda c: c.get("etat") == "attribuee" and c["livreur_id"] == str(id2))
    assert final["livreur_id"] == str(id2)
    await s.plateforme.depot.vider()
    admin_id, admin = await s.utilisateur(Role.ADMINISTRATEUR, "Admin")
    types = [e["type"] for e in (await s.commande(cid, admin))["evenements"]]
    assert "signal_perdu" in types and "reattribution" in types  # tout est dans le journal


async def test_le_retour_du_signal_annule_l_alerte(systeme):
    s = systeme
    _, client = await s.utilisateur(Role.CLIENT, "Awa")
    id1, l1 = await s.utilisateur(Role.LIVREUR, "Premier")
    cid = await commande_attribuee(s, client, l1, pt(1, 0))
    sup = s.plateforme.superviseur
    silence(s, id1, 60)
    assert await sup.passe() == [(cid, "alerte")]
    await s.api.post("/livreurs/moi/position", json=gps(pt(1, 1)), headers=l1)  # le signal revient
    assert await sup.passe() == []
    silence(s, id1, 60)  # un nouveau silence est signalé à nouveau
    assert await sup.passe() == [(cid, "alerte")]


async def test_colis_deja_enleve_jamais_reattribue(systeme):
    s = systeme
    _, client = await s.utilisateur(Role.CLIENT, "Awa")
    id1, l1 = await s.utilisateur(Role.LIVREUR, "Premier")
    cid = await commande_attribuee(s, client, l1, pt(1, 0))
    async with s.fabrique() as session:  # le livreur a enlevé le colis (statut géré au lot 3)
        await session.execute(update(Commande).where(Commande.id == cid).values(etat=EtatCommande.ENLEVEE))
        await session.commit()
    silence(s, id1, 600)
    assert await s.plateforme.superviseur.passe() == [(cid, "alerte")]  # alerte, mais le colis est chez lui
    assert (await s.commande(cid, client))["livreur_id"] == str(id1)


async def test_livreur_deconnecte_en_course_finit_par_etre_remplace(systeme):
    s = systeme
    _, client = await s.utilisateur(Role.CLIENT, "Awa")
    id1, l1 = await s.utilisateur(Role.LIVREUR, "Premier")
    cid = await commande_attribuee(s, client, l1, pt(1, 0))
    sup = s.plateforme.superviseur
    sup.reglages = dataclasses.replace(sup.reglages, alerte_apres_s=0.3, reattribution_apres_s=0.8)
    await s.api.post("/livreurs/moi/deconnexion", headers=l1)  # plus d'agent : le silence est mesuré à partir de là
    assert await sup.passe() == []
    import asyncio
    await asyncio.sleep(0.5)
    assert await sup.passe() == [(cid, "alerte")]
    await asyncio.sleep(0.5)
    assert await sup.passe() == [(cid, "reattribution")]
    assert (await s.attendre(lambda: s.commande(cid, client), lambda c: c.get("etat") == "en_recherche"))["livreur_id"] is None
