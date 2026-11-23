"""Enregistrement en base des événements du Dispatcher, des offres et de l'état des commandes.

Les événements arrivent depuis le Dispatcher (fonction synchrone) : ils sont mis en file et écrits
dans l'ordre par une tâche unique, pour ne jamais ranger « attribuée » avant « offre reçue ».
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import update

from livraison.db.modeles import Commande, EtatCommande, Evenement, Offre, ResultatOffre

journal = logging.getLogger("livraison.depot")


def _uuid(valeur) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(valeur))
    except (ValueError, AttributeError):
        return None


class Depot:
    def __init__(self, fabrique_session):
        self._fabrique = fabrique_session
        self._file: asyncio.Queue = asyncio.Queue()
        self._tache: asyncio.Task | None = None

    def demarrer(self) -> None:
        self._tache = asyncio.create_task(self._travailleur())

    async def vider(self) -> None:
        """Attend que tous les événements reçus soient écrits."""
        await self._file.join()

    async def arreter(self) -> None:
        await self.vider()
        if self._tache:
            self._tache.cancel()
            try:
                await self._tache
            except asyncio.CancelledError:
                pass

    def sur_evenement(self, commande_id: str, type_: str, detail: dict) -> None:
        self._file.put_nowait((commande_id, type_, dict(detail), datetime.now(UTC)))

    async def _travailleur(self) -> None:
        while True:
            element = await self._file.get()
            try:
                await self._ecrire(*element)
            except Exception:  # noqa: BLE001  (une panne d'écriture ne doit pas arrêter les livraisons)
                journal.exception("Écriture de l'événement %s impossible", element[1])
            finally:
                self._file.task_done()

    async def _ecrire(self, commande_id: str, type_: str, detail: dict, quand: datetime) -> None:
        cid = _uuid(commande_id)
        if cid is None:
            return
        livreur = _uuid(detail.get("livreur"))
        async with self._fabrique() as s:
            s.add(Evenement(commande_id=cid, type=type_, detail=json.loads(json.dumps(detail, default=str)),
                            cree_le=quand))
            commande = await s.get(Commande, cid)
            if commande is None:
                await s.commit()
                return
            if type_ in ("soumise", "en_file"):
                commande.etat = EtatCommande.EN_RECHERCHE
            elif type_ == "offre_recue" and livreur:
                s.add(Offre(commande_id=cid, livreur_id=livreur, cout=detail["cout"],
                            arrivee_estimee=datetime.fromisoformat(detail["arrivee"]),
                            resultat=ResultatOffre.PROPOSEE))
            elif type_ in ("confirmation_refusee", "confirmation_absente") and livreur:
                await s.execute(update(Offre).where(
                    Offre.commande_id == cid, Offre.livreur_id == livreur, Offre.resultat == ResultatOffre.PROPOSEE
                ).values(resultat=ResultatOffre.REFUSEE))
            elif type_ == "attribuee" and livreur:
                commande.livreur_id, commande.etat, commande.attribuee_le = livreur, EtatCommande.ATTRIBUEE, quand
                await s.execute(update(Offre).where(
                    Offre.commande_id == cid, Offre.livreur_id == livreur, Offre.resultat == ResultatOffre.PROPOSEE
                ).values(resultat=ResultatOffre.RETENUE))
                await s.execute(update(Offre).where(
                    Offre.commande_id == cid, Offre.resultat == ResultatOffre.PROPOSEE
                ).values(resultat=ResultatOffre.PERDUE))
            elif type_ == "reattribution":
                commande.livreur_id, commande.etat = None, EtatCommande.EN_RECHERCHE
                await s.execute(update(Offre).where(
                    Offre.commande_id == cid, Offre.resultat == ResultatOffre.RETENUE
                ).values(resultat=ResultatOffre.PERDUE))
            elif type_ == "annulee":
                commande.livreur_id, commande.etat = None, EtatCommande.ANNULEE
            await s.commit()
