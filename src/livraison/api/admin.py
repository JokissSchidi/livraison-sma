"""Validation des dossiers livreurs par l'administrateur."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from livraison.api.dependances import exiger_roles
from livraison.db.base import obtenir_session
from livraison.db.modeles import Livreur, Role, Utilisateur, ValidationLivreur, Vehicule

routeur = APIRouter(prefix="/admin", tags=["administration"],
                    dependencies=[Depends(exiger_roles(Role.ADMINISTRATEUR))])


class DossierLivreur(BaseModel):
    utilisateur_id: uuid.UUID
    nom: str
    telephone: str
    vehicule: Vehicule
    capacite_kg: float
    validation: ValidationLivreur


@routeur.get("/livreurs")
async def lister_livreurs(validation: ValidationLivreur = ValidationLivreur.EN_ATTENTE,
                          session: AsyncSession = Depends(obtenir_session)) -> list[DossierLivreur]:
    lignes = await session.execute(
        select(Livreur, Utilisateur)
        .join(Utilisateur, Utilisateur.id == Livreur.utilisateur_id)
        .where(Livreur.validation == validation)
        .order_by(Utilisateur.cree_le)
    )
    return [
        DossierLivreur(utilisateur_id=u.id, nom=u.nom, telephone=u.telephone, vehicule=l.vehicule,
                       capacite_kg=float(l.capacite_kg), validation=l.validation)
        for l, u in lignes.all()
    ]


async def _decider(livreur_id: uuid.UUID, decision: ValidationLivreur, session: AsyncSession) -> DossierLivreur:
    livreur = await session.get(Livreur, livreur_id)
    if livreur is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Livreur introuvable.")
    livreur.validation = decision
    await session.commit()
    u = await session.get(Utilisateur, livreur_id)
    return DossierLivreur(utilisateur_id=u.id, nom=u.nom, telephone=u.telephone, vehicule=livreur.vehicule,
                          capacite_kg=float(livreur.capacite_kg), validation=livreur.validation)


@routeur.post("/livreurs/{livreur_id}/valider")
async def valider(livreur_id: uuid.UUID, session: AsyncSession = Depends(obtenir_session)) -> DossierLivreur:
    return await _decider(livreur_id, ValidationLivreur.VALIDE, session)


@routeur.post("/livreurs/{livreur_id}/refuser")
async def refuser(livreur_id: uuid.UUID, session: AsyncSession = Depends(obtenir_session)) -> DossierLivreur:
    return await _decider(livreur_id, ValidationLivreur.REFUSE, session)
