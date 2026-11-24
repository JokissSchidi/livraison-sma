"""Assemble les agents (Annuaire, Dispatcher, Livreurs), la base de données et les confirmations humaines."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import update

from livraison.agents.annuaire import AnnuaireAgent
from livraison.agents.dispatcher import DispatcherAgent, ReglagesDispatcher, ResultatAttribution
from livraison.agents.gestionnaire import GestionnaireLivreurs, jid_livreur
from livraison.agents.supervision import ReglagesSupervision, Superviseur
from livraison.config import parametres
from livraison.db.modeles import Livreur
from livraison.modele.offre import Coefficients, Demande, Proposition, SituationLivreur
from livraison.services.comptes_xmpp import ComptesEjabberd
from livraison.services import restauration
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
    async def demarrer(self, restaurer: bool = True) -> None:
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
        if restaurer:
            try:
                bilan = await self.restaurer()
                journal.info("Reprise après redémarrage : %s", bilan)
            except Exception:  # noqa: BLE001
                journal.exception("REPRISE APRÈS REDÉMARRAGE ÉCHOUÉE : des commandes en cours peuvent ne pas avoir repris")

    async def restaurer(self) -> dict[str, int]:
        """F-36 : reconstruit agents et commandes en cours à partir de la base de données (NF-04 : aucune commande perdue)."""
        async with self.fabrique_session() as s:
            commandes = await restauration.lire_commandes_en_cours(s)
            livreurs = await restauration.lire_livreurs_en_ligne(s)
        chez_livreur = defaultdict(list)
        for c in commandes:
            if c.etat in restauration.CHEZ_LE_LIVREUR and c.livreur_id:
                chez_livreur[c.livreur_id].append(c)
        bilan = {"livreurs": 0, "commandes_attribuees": 0, "commandes_a_attribuer": 0}

        # 1. Les agents des livreurs qui étaient en ligne, avec leurs courses en cours.
        for lv in livreurs:
            mes_courses = chez_livreur.get(lv.id, [])
            arrets, poids = restauration.arrets_du_livreur(mes_courses)
            situation = SituationLivreur(
                livreur_id=lv.id, vehicule=lv.vehicule, capacite_kg=lv.capacite_kg, position=lv.position,
                position_le=lv.position_le, arrets_restants=arrets, nb_courses=len(mes_courses),
                charge_kg=poids, note=lv.note)
            agent = await self.gestionnaire.connecter(situation)
            agent.courses.update({c.id: c.demande for c in mes_courses})
            bilan["livreurs"] += 1

        # 2. Les commandes déjà attribuées : le Dispatcher les retrouve (la surveillance GPS s'applique de nouveau).
        for livreur_id, mes_courses in chez_livreur.items():
            for c in mes_courses:
                self.dispatcher.restaurer_attribuee(c.id, c.demande, livreur_id, jid_livreur(livreur_id),
                                                    c.urgente, c.cree_le)
                bilan["commandes_attribuees"] += 1

        # 3. Les commandes en recherche (ou créées juste avant l'arrêt) repartent dans la file d'attente.
        for c in commandes:
            if c.etat in restauration.NON_ATTRIBUEES:
                self.depot.sur_evenement(c.id, "reprise", {"etat": c.etat.value})
                self.lancer_attribution(c.id, c.demande, c.urgente)
                bilan["commandes_a_attribuer"] += 1
        return bilan

    async def deconnecter_livreur(self, livreur_id: str) -> bool:
        """Retire l'agent du livreur et le marque hors ligne en base."""
        retire = await self.gestionnaire.deconnecter(livreur_id)
        async with self.fabrique_session() as s:
            await s.execute(update(Livreur).where(Livreur.utilisateur_id == uuid.UUID(livreur_id)).values(en_ligne=False))
            await s.commit()
        return retire

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
