"""Agent Livreur : « jumeau numérique » du livreur (cahier des charges § 3.1)."""
from __future__ import annotations

import dataclasses
import logging
from datetime import UTC, datetime

from spade.agent import Agent
from spade.behaviour import CyclicBehaviour, OneShotBehaviour

from livraison.agents import messages as m
from livraison.modele.offre import Coefficients, Demande, Proposition, SituationLivreur, evaluer_offre
from livraison.services.itineraires import Coord

journal = logging.getLogger("livraison.livreur")


class _Envoi(OneShotBehaviour):
    def __init__(self, msg):
        super().__init__()
        self.msg = msg

    async def run(self):
        await self.send(self.msg)


class LivreurAgent(Agent):
    """Répond aux appels d'offres avec le calcul du coût et tient ses courses à jour.

    Les messages sont traités un par un : deux acceptations simultanées ne peuvent donc pas
    dépasser la capacité du livreur (F-32). À l'acceptation, l'offre est recalculée avec l'état
    du moment, car il a pu changer depuis la proposition.
    """

    def __init__(self, jid, mot_de_passe, situation: SituationLivreur, coef: Coefficients, itineraires,
                 annuaire_jid: str, **kwargs):
        super().__init__(jid, mot_de_passe, **kwargs)
        self.situation = situation
        self.coef = coef
        self.itineraires = itineraires
        self.annuaire_jid = annuaire_jid
        self.courses: dict[str, Demande] = {}  # commande_id -> colis accepté
        self._retire = False

    # ------------------------------------------------------------------ cycle de vie
    async def envoyer(self, msg) -> None:
        comportement = _Envoi(msg)
        self.add_behaviour(comportement)
        await comportement.join()

    async def setup(self):
        self.add_behaviour(self.Ecouter())
        # Pas d'attente ici : un comportement ne démarre qu'une fois setup() terminé.
        self.add_behaviour(_Envoi(m.creer(self.annuaire_jid, m.REQUEST, {
            "action": "enregistrer", "livreur_id": self.situation.livreur_id,
            "vehicule": self.situation.vehicule.value,
            "position": m.coord_vers_json(self.situation.position)})))

    async def stop(self):
        if not self._retire:
            self._retire = True
            try:
                await self.envoyer(m.creer(self.annuaire_jid, m.REQUEST,
                                           {"action": "retirer", "livreur_id": self.situation.livreur_id}))
            except Exception:  # noqa: BLE001  (on s'arrête quand même)
                journal.exception("Impossible de prévenir l'annuaire du départ de %s", self.situation.livreur_id)
        await super().stop()

    # ------------------------------------------------------------------ position GPS
    async def maj_position(self, position: Coord, quand: datetime | None = None) -> None:
        """À appeler à chaque point GPS reçu du téléphone."""
        self.situation = dataclasses.replace(self.situation, position=position,
                                             position_le=quand or datetime.now(UTC))
        await self.envoyer(m.creer(self.annuaire_jid, m.REQUEST, {
            "action": "position", "livreur_id": self.situation.livreur_id,
            "position": m.coord_vers_json(position)}))

    # ------------------------------------------------------------------ protocole Contract Net
    class Ecouter(CyclicBehaviour):
        async def run(self):
            msg = await self.receive(timeout=5)
            if msg is None:
                return
            agent: LivreurAgent = self.agent
            try:
                perf = m.performative(msg)
                if perf == m.CFP:
                    await self.sur_cfp(agent, msg)
                elif perf == m.ACCEPT:
                    await self.sur_acceptation(agent, msg)
                elif perf == m.REJECT:
                    journal.info("Offre non retenue pour la commande %s", msg.thread)
                elif perf == m.CANCEL:
                    await self.sur_annulation(agent, msg)
            except (KeyError, ValueError, TypeError) as e:
                journal.warning("Message invalide ignoré (%s) : %s", type(e).__name__, e)
                await self.send(m.repondre(msg, m.FAILURE, {"erreur": str(e)}))

        async def sur_cfp(self, agent, msg):
            demande = m.demande_depuis_json(m.lire(msg)["demande"])
            resultat = await evaluer_offre(demande, agent.situation, agent.coef, agent.itineraires)
            if isinstance(resultat, Proposition):
                await self.send(m.repondre(msg, m.PROPOSE, m.proposition_vers_json(resultat)))
            else:
                await self.send(m.repondre(msg, m.REFUSE, m.refus_vers_json(resultat)))

        async def sur_annulation(self, agent, msg):
            """La course est retirée du livreur (refus humain, panne, annulation) : sa place est libérée."""
            commande = msg.thread
            demande = agent.courses.pop(commande, None)
            if demande is not None:
                arrets = list(agent.situation.arrets_restants)
                for point in (demande.enlevement, demande.livraison):
                    if point in arrets:
                        arrets.remove(point)
                s = agent.situation
                agent.situation = dataclasses.replace(
                    s, arrets_restants=tuple(arrets), nb_courses=max(0, s.nb_courses - 1),
                    charge_kg=max(0.0, s.charge_kg - demande.poids_kg))
            await self.send(m.repondre(msg, m.INFORM, {"etat": "annule" if demande else "inconnu",
                                                        "commande": commande}))

        async def sur_acceptation(self, agent, msg):
            commande = msg.thread
            demande = m.demande_depuis_json(m.lire(msg)["demande"])
            if commande in agent.courses:  # message reçu deux fois : déjà pris en compte
                await self.send(m.repondre(msg, m.INFORM, {"etat": "accepte", "commande": commande}))
                return
            # Recalcul avec l'état actuel : une autre course a pu être acceptée entre-temps.
            resultat = await evaluer_offre(demande, agent.situation, agent.coef, agent.itineraires)
            if not isinstance(resultat, Proposition):
                await self.send(m.repondre(msg, m.FAILURE, m.refus_vers_json(resultat)))
                return
            s = agent.situation
            agent.situation = dataclasses.replace(
                s, arrets_restants=resultat.arrets_apres, nb_courses=s.nb_courses + 1,
                charge_kg=s.charge_kg + demande.poids_kg)
            agent.courses[commande] = demande
            await self.send(m.repondre(msg, m.INFORM, {"etat": "accepte", "commande": commande,
                                                        "cout": resultat.cout}))
