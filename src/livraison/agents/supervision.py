"""Surveillance du signal GPS des livreurs en course (F-34).

Un livreur qui n'envoie plus de position (téléphone éteint, plus de réseau, panne...) déclenche
d'abord une alerte au répartiteur, puis la réattribution de sa course... tant que le colis n'est
pas encore entre ses mains : une fois enlevé, la commande n'est jamais réattribuée automatiquement
(le colis est avec lui), seul le répartiteur est alerté.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from livraison.db.modeles import Commande, EtatCommande

journal = logging.getLogger("livraison.supervision")

EN_COURSE = (EtatCommande.ATTRIBUEE, EtatCommande.ENLEVEE, EtatCommande.EN_LIVRAISON)


@dataclass(frozen=True)
class ReglagesSupervision:
    periode_s: float = 5.0  # fréquence de la vérification
    alerte_apres_s: float = 45.0  # silence GPS qui déclenche l'alerte au répartiteur
    reattribution_apres_s: float = 120.0  # silence GPS qui déclenche la réattribution
    deconnexion_apres_s: float = 600.0  # livreur en ligne SANS course et sans signal : déconnecté (NF-07)


class Superviseur:
    def __init__(self, plateforme, reglages: ReglagesSupervision | None = None):
        self.plateforme = plateforme
        self.reglages = reglages or ReglagesSupervision()
        self._tache: asyncio.Task | None = None
        self._verrou = asyncio.Lock()
        self._absent_depuis: dict[str, float] = {}  # livreur sans agent : depuis quand (horloge monotone)
        self._alertes: set[str] = set()  # commandes déjà signalées (une alerte par épisode de silence)

    def demarrer(self) -> None:
        self._tache = asyncio.create_task(self._boucle())

    async def arreter(self) -> None:
        if self._tache:
            self._tache.cancel()
            try:
                await self._tache
            except asyncio.CancelledError:
                pass

    async def _boucle(self) -> None:
        while True:
            await asyncio.sleep(self.reglages.periode_s)
            try:
                await self.passe()
            except Exception:  # noqa: BLE001  (la surveillance ne doit jamais s'arrêter)
                journal.exception("Erreur pendant la surveillance")

    def _silence_s(self, livreur_id: str) -> float:
        """Secondes écoulées depuis la dernière position reçue (ou depuis la disparition de son agent)."""
        agent = self.plateforme.agent_livreur(livreur_id)
        if agent is not None:
            self._absent_depuis.pop(livreur_id, None)
            return (datetime.now(UTC) - agent.situation.position_le).total_seconds()
        depuis = self._absent_depuis.setdefault(livreur_id, time.monotonic())
        return time.monotonic() - depuis

    async def passe(self) -> list[tuple[str, str]]:
        """Une vérification. Renvoie les actions faites : [(commande_id, "alerte" | "reattribution")]."""
        r, pf = self.reglages, self.plateforme
        actions: list[tuple[str, str]] = []
        async with self._verrou:
            async with pf.fabrique_session() as s:
                lignes = (await s.execute(
                    select(Commande.id, Commande.livreur_id, Commande.etat)
                    .where(Commande.etat.in_(EN_COURSE), Commande.livreur_id.is_not(None)))).all()
            for commande_id, livreur_uuid, etat in lignes:
                cid, lid = str(commande_id), str(livreur_uuid)
                silence = self._silence_s(lid)
                if silence < r.alerte_apres_s:
                    self._alertes.discard(cid)  # le signal est revenu
                    continue
                if cid not in self._alertes:
                    self._alertes.add(cid)
                    pf._alerter(cid, f"signal GPS du livreur perdu depuis {silence:.0f} s")
                    pf.depot.sur_evenement(cid, "signal_perdu", {"livreur": lid, "depuis_s": round(silence)})
                    actions.append((cid, "alerte"))
                if (silence >= r.reattribution_apres_s and etat == EtatCommande.ATTRIBUEE
                        and cid in pf.dispatcher.suivis):
                    self._alertes.discard(cid)
                    pf.lancer_reattribution(cid, "perte du signal GPS")  # en arrière-plan : on continue de surveiller
                    actions.append((cid, "reattribution"))
            actions += await self._deconnecter_inactifs()
        return actions

    async def _deconnecter_inactifs(self) -> list[tuple[str, str]]:
        """Un livreur sans course dont le téléphone ne donne plus signe de vie n'a plus à être « en ligne »."""
        pf, faites = self.plateforme, []
        for lid, agent in pf.gestionnaire.en_ligne().items():
            silence = (datetime.now(UTC) - agent.situation.position_le).total_seconds()
            if agent.situation.nb_courses == 0 and silence >= self.reglages.deconnexion_apres_s:
                await pf.deconnecter_livreur(lid)
                journal.info("Livreur %s déconnecté : aucun signal depuis %.0f s", lid, silence)
                faites.append((lid, "deconnexion"))
        return faites
