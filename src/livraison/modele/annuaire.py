"""Annuaire des livreurs en ligne (logique pure, sans réseau)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from livraison.db.modeles import Vehicule
from livraison.modele.offre import distance_m
from livraison.services.itineraires import Coord


@dataclass
class EntreeAnnuaire:
    livreur_id: str
    jid: str
    vehicule: Vehicule
    position: Coord
    mis_a_jour_le: datetime


@dataclass(frozen=True)
class Candidat:
    livreur_id: str
    jid: str
    vehicule: Vehicule
    distance_m: float  # à vol d'oiseau, depuis le point cherché


class AnnuaireMemoire:
    def __init__(self) -> None:
        self._entrees: dict[str, EntreeAnnuaire] = {}

    def __len__(self) -> int:
        return len(self._entrees)

    def enregistrer(self, entree: EntreeAnnuaire) -> None:
        self._entrees[entree.livreur_id] = entree

    def retirer(self, livreur_id: str) -> bool:
        return self._entrees.pop(livreur_id, None) is not None

    def maj_position(self, livreur_id: str, position: Coord, quand: datetime) -> bool:
        entree = self._entrees.get(livreur_id)
        if entree is None:
            return False
        entree.position, entree.mis_a_jour_le = position, quand
        return True

    def chercher(
        self,
        centre: Coord,
        rayon_m: float,
        *,
        vehicules: set[Vehicule] | None = None,
        age_max_s: float = 120.0,
        maintenant: datetime | None = None,
    ) -> list[Candidat]:
        """Livreurs dans le rayon, du plus proche au plus éloigné.

        Une entrée dont la position n'a pas été mise à jour depuis `age_max_s` est ignorée :
        un agent disparu sans prévenir sort ainsi de lui-même de l'annuaire.
        """
        maintenant = maintenant or datetime.now(UTC)
        trouves = []
        for e in self._entrees.values():
            if (maintenant - e.mis_a_jour_le).total_seconds() > age_max_s:
                continue
            if vehicules and e.vehicule not in vehicules:
                continue
            d = distance_m(centre, e.position)
            if d <= rayon_m:
                trouves.append(Candidat(e.livreur_id, e.jid, e.vehicule, d))
        return sorted(trouves, key=lambda c: c.distance_m)
