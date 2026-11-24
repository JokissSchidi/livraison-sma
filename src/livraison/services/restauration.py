"""Lecture en base de ce qu'il faut reconstruire au redémarrage (F-36, NF-04)."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from geoalchemy2 import Geometry
from sqlalchemy import cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from livraison.db.modeles import (
    Commande,
    EtatCommande,
    Livreur,
    Position,
    ValidationLivreur,
    Vehicule,
)
from livraison.modele.offre import Demande
from livraison.services.itineraires import Coord

NON_ATTRIBUEES = (EtatCommande.CREEE, EtatCommande.VALIDEE, EtatCommande.EN_RECHERCHE)
CHEZ_LE_LIVREUR = (EtatCommande.ATTRIBUEE, EtatCommande.ENLEVEE, EtatCommande.EN_LIVRAISON)


@dataclass(frozen=True)
class CommandeARestaurer:
    id: str
    etat: EtatCommande
    livreur_id: str | None
    demande: Demande
    urgente: bool
    cree_le: datetime
    attribuee_le: datetime | None


@dataclass(frozen=True)
class LivreurARestaurer:
    id: str
    vehicule: Vehicule
    capacite_kg: float
    note: float | None
    position: Coord
    position_le: datetime


async def lire_commandes_en_cours(session: AsyncSession) -> list[CommandeARestaurer]:
    """Toutes les commandes ni livrées, ni annulées, ni en échec : elles doivent reprendre."""
    enlevement, livraison = (cast(Commande.position_enlevement, Geometry), cast(Commande.position_livraison, Geometry))
    lignes = await session.execute(
        select(Commande, func.ST_Y(enlevement), func.ST_X(enlevement), func.ST_Y(livraison), func.ST_X(livraison))
        .where(Commande.etat.in_(NON_ATTRIBUEES + CHEZ_LE_LIVREUR))
        .order_by(Commande.cree_le))
    return [
        CommandeARestaurer(
            id=str(c.id), etat=c.etat, livreur_id=str(c.livreur_id) if c.livreur_id else None,
            demande=Demande(Coord(lat_e, lon_e), Coord(lat_l, lon_l), float(c.poids_kg)),
            urgente=c.priorite > 0, cree_le=c.cree_le, attribuee_le=c.attribuee_le)
        for c, lat_e, lon_e, lat_l, lon_l in lignes.all()
    ]


async def lire_livreurs_en_ligne(session: AsyncSession) -> list[LivreurARestaurer]:
    """Livreurs qui étaient en ligne, avec leur dernière position connue (ceux sans position sont ignorés)."""
    livreurs = (await session.scalars(
        select(Livreur).where(Livreur.en_ligne.is_(True), Livreur.validation == ValidationLivreur.VALIDE))).all()
    resultat = []
    for lv in livreurs:
        derniere = (await session.execute(
            select(func.ST_Y(cast(Position.position, Geometry)), func.ST_X(cast(Position.position, Geometry)),
                   Position.enregistree_le)
            .where(Position.livreur_id == lv.utilisateur_id)
            .order_by(Position.enregistree_le.desc()).limit(1))).first()
        if derniere is None:
            continue
        resultat.append(LivreurARestaurer(
            id=str(lv.utilisateur_id), vehicule=lv.vehicule, capacite_kg=float(lv.capacite_kg),
            note=float(lv.note) if lv.note is not None else None,
            position=Coord(derniere[0], derniere[1]), position_le=derniere[2]))
    return resultat


def arrets_du_livreur(commandes: list[CommandeARestaurer]) -> tuple[tuple[Coord, ...], float]:
    """Arrêts restants et poids à bord, reconstruits dans l'ordre d'attribution.

    Colis pas encore enlevé : enlèvement puis livraison. Colis déjà enlevé : livraison seulement.
    """
    arrets: list[Coord] = []
    poids = 0.0
    for c in sorted(commandes, key=lambda x: x.attribuee_le or x.cree_le):
        if c.etat == EtatCommande.ATTRIBUEE:
            arrets.append(c.demande.enlevement)
        arrets.append(c.demande.livraison)
        poids += c.demande.poids_kg
    return tuple(arrets), poids


def uuid_ou_none(valeur) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(valeur))
    except (ValueError, AttributeError):
        return None
