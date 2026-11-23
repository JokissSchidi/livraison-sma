"""Commandes : création par le client, suivi, annulation."""
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from livraison.api.dependances import exiger_roles, obtenir_plateforme, utilisateur_courant
from livraison.api.schemas import PointGPS
from livraison.db.base import obtenir_session
from livraison.db.modeles import Commande, EtatCommande, Evenement, Role, Utilisateur
from livraison.modele.offre import Demande
from livraison.services.itineraires import Coord

routeur = APIRouter(prefix="/commandes", tags=["commandes"])

PERSONNEL = (Role.REPARTITEUR, Role.ADMINISTRATEUR)
ANNULABLES = (EtatCommande.CREEE, EtatCommande.VALIDEE, EtatCommande.EN_RECHERCHE, EtatCommande.ATTRIBUEE)


class NouvelleCommande(BaseModel):
    adresse_enlevement: str = Field(min_length=3, max_length=255)
    enlevement: PointGPS
    adresse_livraison: str = Field(min_length=3, max_length=255)
    livraison: PointGPS
    description_colis: str = Field(default="", max_length=255)
    poids_kg: float = Field(gt=0, le=500)
    urgente: bool = False


class CommandeOut(BaseModel):
    id: uuid.UUID
    etat: EtatCommande
    adresse_enlevement: str
    adresse_livraison: str
    poids_kg: float
    priorite: int
    livreur_id: uuid.UUID | None
    cree_le: datetime
    attribuee_le: datetime | None
    evenements: list[dict] | None = None  # réservé au répartiteur et à l'administrateur


def _vers_sortie(c: Commande, evenements: list[dict] | None = None) -> CommandeOut:
    return CommandeOut(id=c.id, etat=c.etat, adresse_enlevement=c.adresse_enlevement,
                       adresse_livraison=c.adresse_livraison, poids_kg=float(c.poids_kg), priorite=c.priorite,
                       livreur_id=c.livreur_id, cree_le=c.cree_le, attribuee_le=c.attribuee_le,
                       evenements=evenements)


def _point(p: PointGPS) -> str:
    return f"SRID=4326;POINT({p.longitude} {p.latitude})"


async def _commande_visible(commande_id: uuid.UUID, utilisateur: Utilisateur, session: AsyncSession) -> Commande:
    commande = await session.get(Commande, commande_id)
    visible = commande is not None and (
        utilisateur.role in PERSONNEL or commande.client_id == utilisateur.id
        or (commande.livreur_id is not None and commande.livreur_id == utilisateur.id))
    if not visible:  # même réponse qu'une commande inexistante : on ne révèle pas l'existence des commandes d'autrui
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Commande introuvable.")
    return commande


@routeur.post("", status_code=status.HTTP_201_CREATED)
async def creer_commande(donnees: NouvelleCommande,
                         client: Utilisateur = Depends(exiger_roles(Role.CLIENT)),
                         session: AsyncSession = Depends(obtenir_session),
                         plateforme=Depends(obtenir_plateforme)) -> CommandeOut:
    commande = Commande(
        client_id=client.id, adresse_enlevement=donnees.adresse_enlevement,
        position_enlevement=_point(donnees.enlevement), adresse_livraison=donnees.adresse_livraison,
        position_livraison=_point(donnees.livraison), description_colis=donnees.description_colis,
        poids_kg=donnees.poids_kg, priorite=1 if donnees.urgente else 0, etat=EtatCommande.CREEE)
    session.add(commande)
    await session.commit()  # la commande existe en base AVANT que l'attribution ne commence
    demande = Demande(Coord(donnees.enlevement.latitude, donnees.enlevement.longitude),
                      Coord(donnees.livraison.latitude, donnees.livraison.longitude), donnees.poids_kg)
    plateforme.lancer_attribution(str(commande.id), demande, donnees.urgente)
    return _vers_sortie(commande)


@routeur.get("")
async def lister_commandes(utilisateur: Utilisateur = Depends(utilisateur_courant),
                           session: AsyncSession = Depends(obtenir_session)) -> list[CommandeOut]:
    requete = select(Commande).order_by(Commande.cree_le.desc()).limit(100)
    if utilisateur.role not in PERSONNEL:
        requete = requete.where(Commande.client_id == utilisateur.id)
    return [_vers_sortie(c) for c in (await session.scalars(requete)).all()]


@routeur.get("/{commande_id}")
async def voir_commande(commande_id: uuid.UUID, utilisateur: Utilisateur = Depends(utilisateur_courant),
                        session: AsyncSession = Depends(obtenir_session)) -> CommandeOut:
    commande = await _commande_visible(commande_id, utilisateur, session)
    evenements = None
    if utilisateur.role in PERSONNEL:
        lignes = await session.scalars(
            select(Evenement).where(Evenement.commande_id == commande_id).order_by(Evenement.id))
        evenements = [{"type": e.type, "detail": e.detail, "cree_le": e.cree_le.isoformat()} for e in lignes]
    return _vers_sortie(commande, evenements)


@routeur.post("/{commande_id}/annuler")
async def annuler_commande(commande_id: uuid.UUID,
                           client: Utilisateur = Depends(exiger_roles(Role.CLIENT)),
                           session: AsyncSession = Depends(obtenir_session),
                           plateforme=Depends(obtenir_plateforme)) -> CommandeOut:
    commande = await _commande_visible(commande_id, client, session)
    if commande.etat not in ANNULABLES:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Une commande « {commande.etat.value} » ne peut plus être annulée.")
    await plateforme.annuler(str(commande_id))
    await plateforme.depot.vider()  # l'état « annulée » est écrit par le dépôt
    await session.refresh(commande)
    if commande.etat != EtatCommande.ANNULEE:  # commande encore inconnue du Dispatcher (jamais soumise)
        commande.etat = EtatCommande.ANNULEE
        await session.commit()
    return _vers_sortie(commande)
