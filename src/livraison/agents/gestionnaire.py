"""Création et suppression dynamiques des agents Livreur (F-30)."""
from __future__ import annotations

import asyncio
import logging

from livraison.agents.livreur import LivreurAgent
from livraison.config import parametres
from livraison.modele.offre import Coefficients, SituationLivreur
from livraison.services.comptes_xmpp import ComptesEjabberd

journal = logging.getLogger("livraison.gestionnaire")


def jid_livreur(livreur_id: str) -> str:
    return f"livreur-{livreur_id}@{parametres.xmpp_domaine}"


class GestionnaireLivreurs:
    """Un livreur passe en ligne : son agent est créé ; il se déconnecte : l'agent est arrêté."""

    def __init__(self, annuaire_jid: str, itineraires, coef: Coefficients | None = None, comptes=None):
        self.annuaire_jid = annuaire_jid
        self.itineraires = itineraires
        self.coef = coef or Coefficients()
        self.comptes = comptes or ComptesEjabberd()
        self._agents: dict[str, LivreurAgent] = {}
        self._verrou = asyncio.Lock()

    def __len__(self) -> int:
        return len(self._agents)

    def agent(self, livreur_id: str) -> LivreurAgent | None:
        return self._agents.get(livreur_id)

    async def connecter(self, situation: SituationLivreur) -> LivreurAgent:
        """Démarre l'agent du livreur (sans effet si déjà en ligne : l'agent existant est renvoyé)."""
        async with self._verrou:
            existant = self._agents.get(situation.livreur_id)
            if existant is not None:
                return existant
            jid = jid_livreur(situation.livreur_id)
            await self.comptes.assurer(jid.split("@")[0])
            agent = LivreurAgent(jid, parametres.xmpp_mot_de_passe_agents, situation, self.coef,
                                 self.itineraires, self.annuaire_jid, port=parametres.xmpp_port)
            await agent.start(auto_register=False)
            self._agents[situation.livreur_id] = agent
            journal.info("Agent %s démarré", jid)
            return agent

    async def deconnecter(self, livreur_id: str) -> bool:
        async with self._verrou:
            agent = self._agents.pop(livreur_id, None)
        if agent is None:
            return False
        await agent.stop()
        journal.info("Agent de %s arrêté", livreur_id)
        return True

    async def tout_arreter(self) -> None:
        for livreur_id in list(self._agents):
            await self.deconnecter(livreur_id)
