"""Calcul du coût d'une offre de livraison (cahier des charges, § 3.2).

    coût = durée(position -> enlèvement) + durée du détour + pénalité × charge

- approche : durée directe de la position actuelle du livreur au point d'enlèvement ;
- détour   : temps que la nouvelle course ajoute à la tournée déjà acceptée, une fois retirés le
             trajet enlèvement -> livraison (identique pour tous les livreurs du même véhicule)
             et l'approche. Nul pour un livreur libre ; négatif (donc réduit le coût) si le colis
             est « sur le chemin » du livreur. Le coût total reste toujours >= 0 ;
- charge   : nombre de courses déjà acceptées, multiplié par la pénalité (en secondes).

Le livreur refuse (Refus) s'il est saturé, si le colis est trop lourd, si sa position GPS est
trop ancienne ou si la course sort de la zone de service.

Ce module ne fait aucune entrée/sortie : le moteur d'itinéraires lui est fourni (voir
`MoteurItineraires`), ce qui permet de le tester sans OSRM.
"""
from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from livraison.db.modeles import Vehicule
from livraison.services.itineraires import (
    AucunItineraire,
    Coord,
    ErreurItineraire,
    Trajet,
)

INFINI = math.inf


class MoteurItineraires(Protocol):
    async def matrice(
        self, sources: list[Coord], destinations: list[Coord], vehicule: Vehicule = ...
    ) -> list[list[Trajet | None]]: ...


class MotifRefus(enum.StrEnum):
    SATURE = "sature"
    TROP_LOURD = "trop_lourd"
    GPS_PERIME = "gps_perime"
    HORS_ZONE = "hors_zone"
    AUCUN_ITINERAIRE = "aucun_itineraire"
    MOTEUR_INDISPONIBLE = "moteur_indisponible"


@dataclass(frozen=True)
class Zone:
    """Zone de service circulaire (les polygones viendront avec F-43)."""

    centre: Coord
    rayon_m: float

    def contient(self, point: Coord) -> bool:
        return distance_m(self.centre, point) <= self.rayon_m


@dataclass(frozen=True)
class Coefficients:
    """Paramètres réglables par l'administrateur."""

    penalite_charge_s: float = 180.0  # secondes ajoutées au coût par course déjà acceptée
    max_courses: int = 3  # au-delà : livreur saturé
    age_gps_max_s: float = 120.0  # position GPS plus vieille que ça : refus
    zone: Zone | None = None  # None : pas de contrôle de zone
    tolerance_egalite_s: float = 1.0  # deux coûts à moins d'une seconde sont « égaux »


@dataclass(frozen=True)
class Demande:
    """Le colis à attribuer (contenu de l'appel d'offres)."""

    enlevement: Coord
    livraison: Coord
    poids_kg: float


@dataclass(frozen=True)
class SituationLivreur:
    """Ce que l'agent Livreur sait de lui-même."""

    livreur_id: str
    vehicule: Vehicule
    capacite_kg: float
    position: Coord
    position_le: datetime  # horodatage du dernier point GPS reçu
    arrets_restants: tuple[Coord, ...] = ()  # points restant à visiter pour les courses acceptées, dans l'ordre
    nb_courses: int = 0  # courses acceptées et non terminées
    charge_kg: float = 0.0  # poids de ces courses
    note: float | None = None  # sert à départager deux offres égales


@dataclass(frozen=True)
class Proposition:
    livreur_id: str
    cout: float  # secondes
    approche_s: float
    detour_s: float
    penalite_s: float
    arrivee_estimee: datetime  # heure d'arrivée au point d'enlèvement
    note: float | None = None


@dataclass(frozen=True)
class Refus:
    livreur_id: str
    motif: MotifRefus
    detail: str = ""


def distance_m(a: Coord, b: Coord) -> float:
    """Distance à vol d'oiseau (formule de haversine), en mètres."""
    r = 6_371_000.0
    p1, p2 = math.radians(a.latitude), math.radians(b.latitude)
    dp = p2 - p1
    dl = math.radians(b.longitude - a.longitude)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _verifier_regles(
    demande: Demande, livreur: SituationLivreur, coef: Coefficients, maintenant: datetime
) -> Refus | None:
    """Refus qui ne demandent aucun calcul d'itinéraire (rapides, donc faits en premier)."""
    lid = livreur.livreur_id
    if livreur.nb_courses >= coef.max_courses:
        return Refus(lid, MotifRefus.SATURE, f"{livreur.nb_courses} courses en cours (maximum {coef.max_courses})")
    if demande.poids_kg > livreur.capacite_kg:
        return Refus(lid, MotifRefus.TROP_LOURD, f"colis {demande.poids_kg} kg > capacité {livreur.capacite_kg} kg")
    if livreur.charge_kg + demande.poids_kg > livreur.capacite_kg:
        return Refus(lid, MotifRefus.TROP_LOURD,
                     f"{livreur.charge_kg} kg déjà à bord + {demande.poids_kg} kg > capacité {livreur.capacite_kg} kg")
    age = (maintenant - livreur.position_le).total_seconds()
    if age > coef.age_gps_max_s:
        return Refus(lid, MotifRefus.GPS_PERIME, f"position vieille de {age:.0f} s (maximum {coef.age_gps_max_s:.0f} s)")
    if coef.zone is not None and not (coef.zone.contient(demande.enlevement) and coef.zone.contient(demande.livraison)):
        return Refus(lid, MotifRefus.HORS_ZONE, "enlèvement ou livraison hors de la zone de service")
    return None


def _meilleure_insertion(d: list[list[float]], n: int) -> tuple[float, float, float]:
    """Insère enlèvement (E) puis livraison (L) dans la tournée au meilleur endroit.

    Indices de la matrice d : 0 = position, 1..n = arrêts restants, n+1 = E, n+2 = L.
    Renvoie (ajout_total_s, duree_jusqu_a_E_s, duree_E_vers_L_s) ; ajout_total = INFINI si impossible.
    """
    e, l = n + 1, n + 2
    route = list(range(n + 1))  # 0, 1, ..., n

    def duree(seq: list[int]) -> float:
        return sum(d[a][b] for a, b in zip(seq, seq[1:], strict=False))

    actuelle = duree(route)
    meilleur = (INFINI, INFINI)
    for i in range(n + 1):  # E est inséré après route[i]
        for j in range(i, n + 1):  # L est inséré après route[j] (j >= i : E avant L)
            seq = route[: i + 1] + [e] + route[i + 1 : j + 1] + [l] + route[j + 1 :]
            total = duree(seq)
            if total - actuelle < meilleur[0]:
                jusqu_a_e = duree(seq[: seq.index(e) + 1])
                meilleur = (total - actuelle, jusqu_a_e)
    return meilleur[0], meilleur[1], d[e][l]


async def evaluer_offre(
    demande: Demande,
    livreur: SituationLivreur,
    coef: Coefficients,
    itineraires: MoteurItineraires,
    maintenant: datetime | None = None,
) -> Proposition | Refus:
    """Calcule l'offre d'un livreur pour un colis, ou le motif de son refus."""
    maintenant = maintenant or datetime.now(UTC)
    refus = _verifier_regles(demande, livreur, coef, maintenant)
    if refus:
        return refus

    n = len(livreur.arrets_restants)
    points = [livreur.position, *livreur.arrets_restants, demande.enlevement, demande.livraison]
    try:
        matrice = await itineraires.matrice(points, points, livreur.vehicule)  # une seule requête
    except AucunItineraire as e:
        return Refus(livreur.livreur_id, MotifRefus.AUCUN_ITINERAIRE, str(e))
    except ErreurItineraire as e:
        return Refus(livreur.livreur_id, MotifRefus.MOTEUR_INDISPONIBLE, str(e))

    d = [[INFINI if c is None else c.duree_s for c in ligne] for ligne in matrice]
    ajout, jusqu_a_e, e_vers_l = _meilleure_insertion(d, n)
    approche = d[0][n + 1]
    if math.isinf(ajout) or math.isinf(approche) or math.isinf(e_vers_l):
        return Refus(livreur.livreur_id, MotifRefus.AUCUN_ITINERAIRE, "point inaccessible par la route")

    detour = ajout - e_vers_l - approche
    penalite = coef.penalite_charge_s * livreur.nb_courses
    cout = max(0.0, approche + detour) + penalite
    return Proposition(
        livreur_id=livreur.livreur_id,
        cout=cout,
        approche_s=approche,
        detour_s=detour,
        penalite_s=penalite,
        arrivee_estimee=maintenant + timedelta(seconds=jusqu_a_e),
        note=livreur.note,
    )


def classer(propositions: list[Proposition], coef: Coefficients) -> list[Proposition]:
    """Trie les offres de la meilleure à la moins bonne.

    Coût croissant ; à coût égal (à la tolérance près), meilleure note d'abord, puis arrivée la plus tôt.
    """
    if not propositions:
        return []
    ordre = sorted(propositions, key=lambda p: p.cout)
    groupes: list[list[Proposition]] = []
    for p in ordre:
        if groupes and p.cout - groupes[-1][0].cout <= coef.tolerance_egalite_s:
            groupes[-1].append(p)
        else:
            groupes.append([p])
    resultat: list[Proposition] = []
    for g in groupes:
        resultat += sorted(g, key=lambda p: (-(p.note if p.note is not None else -1.0), p.arrivee_estimee))
    return resultat
