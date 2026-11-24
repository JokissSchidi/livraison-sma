"""Assemble les agents (Annuaire, Dispatcher, Livreurs), la base de données et les confirmations humaines."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from livraison.agents.annuaire import AnnuaireAgent
from livraison.agents.dispatcher import DispatcherAgent, ReglagesDispatcher, ResultatAttribution
from livraison.agents.gestionnaire import GestionnaireLivreurs
from livraison.agents.supervision import ReglagesSupervision, Superviseur
from livraison.config import parametres
from livraison.modele.offre import Coefficients, Demande, Proposition, SituationLivreur
from livraison.services.comptes_xmpp import ComptesEjabberd
from livraison.services.depot import Depot
from livraison.services.itineraires import ClientItineraires

journal = logging.getLogger("livraison.plateforme")


@dataclass
class PropositionEnAttente:
    """Une course proposée à un livreur, en attente de sa réponse."""

    livreur_id: str
    commande_id: str
    demande: Demande
    proposition: Proposition
    delai_s: float
    recue_a: float = field(default_factory=time.monotonic)
    reponse: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())

    def secondes_restantes(self) -> float:
        return max(0.0, self.delai_s - (time.monotonic() - self.recue_a))


class Plateforme:
    def __init__(self, fabrique_session, *, comptes=None, itineraires=None,
                 reglages: ReglagesDispatcher | None = None, coef: Coefficients | None = None,
                 prefixe_jid: str = "", supervision: ReglagesSupervision | None = None):
        self.fabrique_session = fabrique_session
        self.comptes = comptes or ComptesEjabberd()
        self.itineraires = itineraires or ClientItineraires.depuis_config()
        self.coef = coef or Coefficients()
        self.reglages = reglages or ReglagesDispatcher(coef=self.coef)
        self.prefixe = prefixe_jid
        self.depot = Depot(fabrique_session)
        self.superviseur = Superviseur(self, supervision)
        self.alertes: list[tuple[str, str]] = []  # commandes à traiter par le répartiteur
        self.annuaire: AnnuaireAgent | None = None
        self.dispatcher: DispatcherAgent | None = None
        self.gestionnaire: GestionnaireLivreurs | None = None
        self._attentes: dict[tuple[str, str], PropositionEnAttente] = {}
        self._taches: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ démarrage / arrêt
    async def demarrer(self) -> None:
        d, mdp, port = parametres.xmpp_domaine, parametres.xmpp_mot_de_passe_agents, parametres.xmpp_port
        noms = (f"{self.prefixe}annuaire", f"{self.prefixe}dispatcher")
        for nom in noms:
            await self.comptes.assurer(nom)
        self.depot.demarrer()
        self.annuaire = AnnuaireAgent(f"{noms[0]}@{d}", mdp, port=port)
        await self.annuaire.start(auto_register=False)
        self.dispatcher = DispatcherAgent(
            f"{noms[1]}@{d}", mdp, f"{noms[0]}@{d}", self.reglages, confirmer=self.confirmer,
            sur_evenement=self.depot.sur_evenement, signaler=self._alerter, port=port)
        await self.dispatcher.start(auto_register=False)
        self.gestionnaire = GestionnaireLivreurs(f"{noms[0]}@{d}", self.itineraires, self.coef, self.comptes)
        self.superviseur.demarrer()
        journal.info("Plateforme multi-agents démarrée")

    async def arreter(self) -> None:
        await self.superviseur.arreter()
        for tache in list(self._taches):
            tache.cancel()
        for attente in self._attentes.values():
            if not attente.reponse.done():
                attente.reponse.set_result(False)
        if self.gestionnaire:
            await self.gestionnaire.tout_arreter()
        if self.dispatcher:
            await self.dispatcher.stop()
        if self.annuaire:
            await self.annuaire.stop()
        await self.depot.arreter()
        close = getattr(self.itineraires, "fermer", None)
        if close:
            await close()

    def _alerter(self, commande_id: str, raison: str) -> None:
        journal.warning("ALERTE répartiteur : commande %s — %s", commande_id, raison)
        self.alertes.append((commande_id, raison))

    # ------------------------------------------------------------------ commandes
    def lancer_attribution(self, commande_id: str, demande: Demande, urgente: bool = False) -> None:
        """Lance l'attribution en arrière-plan (l'API répond tout de suite)."""
        tache = asyncio.create_task(self.dispatcher.soumettre(commande_id, demande, urgente))
        self._taches.add(tache)
        tache.add_done_callback(self._taches.discard)

    def lancer_reattribution(self, commande_id: str, raison: str) -> None:
        """Relance l'appel d'offres en arrière-plan (la confirmation humaine peut durer 30 s : la surveillance ne doit pas attendre)."""
        tache = asyncio.create_task(self.dispatcher.reattribuer(commande_id, raison))
        self._taches.add(tache)
        tache.add_done_callback(self._taches.discard)

    async def annuler(self, commande_id: str) -> None:
        await self.dispatcher.annuler(commande_id)

    # ------------------------------------------------------------------ livreurs
    async def livreur_en_ligne(self, situation: SituationLivreur) -> None:
        await self.gestionnaire.connecter(situation)
        fin = time.monotonic() + 3.0  # attendre qu'il soit inscrit à l'annuaire avant de relancer la file
        while situation.livreur_id not in self.annuaire.annuaire._entrees and time.monotonic() < fin:
            await asyncio.sleep(0.05)
        self.dispatcher.reveiller()

    async def livreur_hors_ligne(self, livreur_id: str) -> bool:
        return await self.gestionnaire.deconnecter(livreur_id)

    def agent_livreur(self, livreur_id: str):
        return self.gestionnaire.agent(livreur_id)

    # ------------------------------------------------------------------ confirmation humaine
    async def confirmer(self, livreur_id: str, commande_id: str, demande: Demande,
                        proposition: Proposition, delai_s: float) -> bool:
        """Appelée par le Dispatcher : attend la réponse du livreur (via l'API) dans le délai imparti."""
        attente = PropositionEnAttente(livreur_id, commande_id, demande, proposition, delai_s)
        self._attentes[(livreur_id, commande_id)] = attente
        try:
            return await attente.reponse
        finally:
            self._attentes.pop((livreur_id, commande_id), None)

    def propositions_en_attente(self, livreur_id: str) -> list[PropositionEnAttente]:
        return [a for (lid, _), a in self._attentes.items() if lid == livreur_id and not a.reponse.done()]

    def repondre(self, livreur_id: str, commande_id: str, accepte: bool) -> bool:
        attente = self._attentes.get((livreur_id, commande_id))
        if attente is None or attente.reponse.done():
            return False
        attente.reponse.set_result(accepte)
        return True
