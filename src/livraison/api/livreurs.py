"""Espace du livreur : connexion, position GPS, propositions de courses."""
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from livraison.api.dependances import exiger_roles, obtenir_plateforme
from livraison.api.schemas import PointGPS
from livraison.db.base import obtenir_session
from livraison.db.modeles import Commande, Livreur, Position, Role, Utilisateur, ValidationLivreur
from livraison.modele.offre import SituationLivreur
from livraison.services.itineraires import Coord

routeur = APIRouter(prefix="/livreurs/moi", tags=["livreur"])
LIVREUR = Depends(exiger_roles(Role.LIVREUR))


class PositionIn(PointGPS):
    precision_m: float | None = None


class PropositionOut(BaseModel):
    commande_id: uuid.UUID
    adresse_enlevement: str
    adresse_livraison: str
    enlevement: PointGPS
    livraison: PointGPS
    poids_kg: float
    duree_approche_s: float
    secondes_restantes: float


@routeur.post("/connexion")
async def connexion(position: PositionIn, livreur_user: Utilisateur = LIVREUR,
                    session: AsyncSession = Depends(obtenir_session), plateforme=Depends(obtenir_plateforme)) -> dict:
    livreur = await session.get(Livreur, livreur_user.id)
    if livreur is None or livreur.validation != ValidationLivreur.VALIDE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Votre dossier n'est pas encore validé : aucune course ne peut vous être proposée.")
    situation = SituationLivreur(
        livreur_id=str(livreur_user.id), vehicule=livreur.vehicule, capacite_kg=float(livreur.capacite_kg),
        position=Coord(position.latitude, position.longitude), position_le=datetime.now(UTC),
        note=float(livreur.note) if livreur.note is not None else None)
    await plateforme.livreur_en_ligne(situation)
    livreur.en_ligne = True
    await session.commit()
    return {"etat": "en_ligne"}


@routeur.post("/deconnexion")
async def deconnexion(livreur_user: Utilisateur = LIVREUR, session: AsyncSession = Depends(obtenir_session),
                      plateforme=Depends(obtenir_plateforme)) -> dict:
    await plateforme.livreur_hors_ligne(str(livreur_user.id))
    livreur = await session.get(Livreur, livreur_user.id)
    if livreur is not None:
        livreur.en_ligne = False
        await session.commit()
    return {"etat": "hors_ligne"}


@routeur.post("/position")
async def envoyer_position(position: PositionIn, livreur_user: Utilisateur = LIVREUR,
                           session: AsyncSession = Depends(obtenir_session),
                           plateforme=Depends(obtenir_plateforme)) -> dict:
    agent = plateforme.agent_livreur(str(livreur_user.id))
    if agent is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Vous n'êtes pas connecté : appelez /livreurs/moi/connexion.")
    await agent.maj_position(Coord(position.latitude, position.longitude))
    session.add(Position(livreur_id=livreur_user.id, precision_m=position.precision_m,
                         position=f"SRID=4326;POINT({position.longitude} {position.latitude})"))
    await session.commit()
    return {"etat": "enregistree"}


@routeur.get("/propositions")
async def propositions(livreur_user: Utilisateur = LIVREUR, session: AsyncSession = Depends(obtenir_session),
                       plateforme=Depends(obtenir_plateforme)) -> list[PropositionOut]:
    """Courses proposées en attente de votre réponse (l'application les interroge régulièrement)."""
    resultat = []
    for attente in plateforme.propositions_en_attente(str(livreur_user.id)):
        commande = await session.get(Commande, uuid.UUID(attente.commande_id))
        if commande is None:
            continue
        d = attente.demande
        resultat.append(PropositionOut(
            commande_id=commande.id, adresse_enlevement=commande.adresse_enlevement,
            adresse_livraison=commande.adresse_livraison,
            enlevement=PointGPS(latitude=d.enlevement.latitude, longitude=d.enlevement.longitude),
            livraison=PointGPS(latitude=d.livraison.latitude, longitude=d.livraison.longitude),
            poids_kg=d.poids_kg, duree_approche_s=attente.proposition.approche_s,
            secondes_restantes=attente.secondes_restantes()))
    return resultat


async def _repondre(commande_id: uuid.UUID, accepte: bool, livreur_user: Utilisateur, plateforme) -> dict:
    if not plateforme.repondre(str(livreur_user.id), str(commande_id), accepte):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Aucune proposition en attente pour cette commande (délai dépassé ?).")
    return {"reponse": "acceptee" if accepte else "declinee"}


@routeur.post("/propositions/{commande_id}/accepter")
async def accepter(commande_id: uuid.UUID, livreur_user: Utilisateur = LIVREUR, plateforme=Depends(obtenir_plateforme)) -> dict:
    return await _repondre(commande_id, True, livreur_user, plateforme)


@routeur.post("/propositions/{commande_id}/decliner")
async def decliner(commande_id: uuid.UUID, livreur_user: Utilisateur = LIVREUR, plateforme=Depends(obtenir_plateforme)) -> dict:
    return await _repondre(commande_id, False, livreur_user, plateforme)
