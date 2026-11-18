"""Inscription, connexion par code SMS et profil."""
import re
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from livraison.api.dependances import utilisateur_courant
from livraison.db.base import obtenir_session
from livraison.db.modeles import (
    CodeVerification,
    EtatCompte,
    Livreur,
    Role,
    Utilisateur,
    Vehicule,
)
from livraison.services import securite
from livraison.services.sms import envoyer_sms

routeur = APIRouter(prefix="/auth", tags=["authentification"])

MOTIF_TELEPHONE = re.compile(r"^\+\d{8,15}$")


def normaliser_telephone(valeur: str) -> str:
    nettoye = re.sub(r"[\s.\-()]", "", valeur)
    if not MOTIF_TELEPHONE.match(nettoye):
        raise ValueError("Numéro invalide : utilisez le format international, par ex. +2250700000000.")
    return nettoye


class Telephone(BaseModel):
    telephone: str

    _normaliser = field_validator("telephone")(lambda cls, v: normaliser_telephone(v))


class Inscription(Telephone):
    nom: str
    role: Role
    vehicule: Vehicule | None = None
    capacite_kg: float | None = None

    @field_validator("role")
    @classmethod
    def role_autorise(cls, v: Role) -> Role:
        if v not in (Role.CLIENT, Role.LIVREUR):
            raise ValueError("Seuls les rôles client et livreur peuvent s'inscrire eux-mêmes.")
        return v

    @field_validator("nom")
    @classmethod
    def nom_non_vide(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Le nom est obligatoire.")
        return v

    @model_validator(mode="after")
    def livreur_complet(self):
        if self.role == Role.LIVREUR and (self.vehicule is None or not self.capacite_kg):
            raise ValueError("Un livreur doit indiquer son véhicule et sa capacité (kg).")
        return self


class Verification(Telephone):
    code: str


class Jeton(BaseModel):
    access_token: str
    token_type: str = "bearer"
    utilisateur_id: uuid.UUID
    role: Role


class Profil(BaseModel):
    id: uuid.UUID
    telephone: str
    nom: str
    role: Role
    validation_livreur: str | None = None


async def emettre_code(session: AsyncSession, telephone: str) -> None:
    """Crée un code, l'enregistre haché et l'envoie par SMS (limite : un envoi par 30 s)."""
    dernier = await session.scalar(
        select(CodeVerification)
        .where(CodeVerification.telephone == telephone)
        .order_by(CodeVerification.cree_le.desc())
        .limit(1)
    )
    if dernier and datetime.now(UTC) - dernier.cree_le < securite.DELAI_RENVOI:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Patientez quelques secondes avant de redemander un code.")
    code = securite.generer_code()
    session.add(
        CodeVerification(
            telephone=telephone,
            code_hash=securite.hacher_code(telephone, code),
            expire_le=datetime.now(UTC) + securite.DUREE_CODE,
        )
    )
    await session.commit()
    await envoyer_sms(telephone, f"Livraison SMA : votre code est {code} (valable 5 minutes).")


@routeur.post("/inscription", status_code=status.HTTP_201_CREATED)
async def inscription(donnees: Inscription, session: AsyncSession = Depends(obtenir_session)) -> dict:
    existant = await session.scalar(select(Utilisateur).where(Utilisateur.telephone == donnees.telephone))
    if existant:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ce numéro est déjà inscrit.")
    utilisateur = Utilisateur(telephone=donnees.telephone, nom=donnees.nom, role=donnees.role)
    session.add(utilisateur)
    await session.flush()
    if donnees.role == Role.LIVREUR:
        session.add(Livreur(utilisateur_id=utilisateur.id, vehicule=donnees.vehicule,
                            capacite_kg=donnees.capacite_kg))
    await session.commit()
    await emettre_code(session, donnees.telephone)
    return {"message": "Compte créé. Un code vous a été envoyé par SMS."}


@routeur.post("/code", status_code=status.HTTP_202_ACCEPTED)
async def demander_code(donnees: Telephone, session: AsyncSession = Depends(obtenir_session)) -> dict:
    """Connexion : demande d'un code. Même réponse que le numéro existe ou non."""
    utilisateur = await session.scalar(select(Utilisateur).where(Utilisateur.telephone == donnees.telephone))
    if utilisateur and utilisateur.etat == EtatCompte.ACTIF:
        await emettre_code(session, donnees.telephone)
    return {"message": "Si le numéro est inscrit, un code vient de lui être envoyé."}


@routeur.post("/verifier")
async def verifier_code(donnees: Verification, session: AsyncSession = Depends(obtenir_session)) -> Jeton:
    invalide = HTTPException(status.HTTP_400_BAD_REQUEST, "Code invalide ou expiré.")
    enregistrement = await session.scalar(
        select(CodeVerification)
        .where(CodeVerification.telephone == donnees.telephone, CodeVerification.utilise.is_(False))
        .order_by(CodeVerification.cree_le.desc())
        .limit(1)
    )
    if enregistrement is None or enregistrement.expire_le < datetime.now(UTC):
        raise invalide
    if enregistrement.tentatives >= securite.MAX_TENTATIVES:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Trop d'essais : demandez un nouveau code.")
    attendu = securite.hacher_code(donnees.telephone, donnees.code.strip())
    if not securite.hmac.compare_digest(attendu, enregistrement.code_hash):
        enregistrement.tentatives += 1
        await session.commit()
        raise invalide
    utilisateur = await session.scalar(select(Utilisateur).where(Utilisateur.telephone == donnees.telephone))
    if utilisateur is None:
        raise invalide
    if utilisateur.etat != EtatCompte.ACTIF:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Compte suspendu.")
    enregistrement.utilise = True
    await session.commit()
    return Jeton(
        access_token=securite.creer_jeton(utilisateur.id, utilisateur.role.value),
        utilisateur_id=utilisateur.id,
        role=utilisateur.role,
    )


@routeur.get("/moi")
async def moi(utilisateur: Utilisateur = Depends(utilisateur_courant),
              session: AsyncSession = Depends(obtenir_session)) -> Profil:
    validation = None
    if utilisateur.role == Role.LIVREUR:
        livreur = await session.get(Livreur, utilisateur.id)
        validation = livreur.validation.value if livreur else None
    return Profil(id=utilisateur.id, telephone=utilisateur.telephone, nom=utilisateur.nom,
                  role=utilisateur.role, validation_livreur=validation)
