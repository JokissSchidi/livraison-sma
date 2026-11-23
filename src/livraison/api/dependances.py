"""Dépendances communes : utilisateur connecté et contrôle des rôles."""
import uuid

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from livraison.db.base import obtenir_session
from livraison.db.modeles import EtatCompte, Role, Utilisateur
from livraison.services.securite import lire_jeton

schema_bearer = HTTPBearer(auto_error=False)


async def utilisateur_courant(
    identifiants: HTTPAuthorizationCredentials | None = Depends(schema_bearer),
    session: AsyncSession = Depends(obtenir_session),
) -> Utilisateur:
    erreur = HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentification requise.",
                           headers={"WWW-Authenticate": "Bearer"})
    if identifiants is None:
        raise erreur
    try:
        charge = lire_jeton(identifiants.credentials)
        utilisateur = await session.get(Utilisateur, uuid.UUID(charge["sub"]))
    except (jwt.PyJWTError, ValueError, KeyError):
        raise erreur from None
    if utilisateur is None:
        raise erreur
    if utilisateur.etat != EtatCompte.ACTIF:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Compte suspendu.")
    return utilisateur


def exiger_roles(*roles: Role):
    """Dépendance qui n'autorise que les rôles indiqués."""

    async def verifier(utilisateur: Utilisateur = Depends(utilisateur_courant)) -> Utilisateur:
        if utilisateur.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Droits insuffisants.")
        return utilisateur

    return verifier


def obtenir_plateforme(request: Request):
    """La plateforme multi-agents démarrée avec l'API ; 503 si elle n'a pas pu démarrer (XMPP arrêté...)."""
    plateforme = getattr(request.app.state, "plateforme", None)
    if plateforme is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "La plateforme d'attribution n'est pas disponible (serveur XMPP arrêté ?).")
    return plateforme
