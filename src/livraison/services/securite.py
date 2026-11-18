"""Codes à usage unique et jetons JWT."""
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

import jwt

from livraison.config import parametres

DUREE_CODE = timedelta(minutes=5)
DUREE_JETON = timedelta(hours=12)
MAX_TENTATIVES = 5
DELAI_RENVOI = timedelta(seconds=30)
ALGORITHME = "HS256"


def _cle() -> str:
    if len(parametres.jwt_secret) < 32:
        raise RuntimeError(
            "JWT_SECRET absent ou trop court. Générez-en un dans .env avec : "
            'python -c "import secrets; print(secrets.token_hex(32))"'
        )
    return parametres.jwt_secret


def generer_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def hacher_code(telephone: str, code: str) -> str:
    return hmac.new(_cle().encode(), f"{telephone}:{code}".encode(), hashlib.sha256).hexdigest()


def creer_jeton(utilisateur_id, role: str) -> str:
    maintenant = datetime.now(UTC)
    charge = {
        "sub": str(utilisateur_id),
        "role": role,
        "iat": maintenant,
        "exp": maintenant + DUREE_JETON,
    }
    return jwt.encode(charge, _cle(), algorithm=ALGORITHME)


def lire_jeton(jeton: str) -> dict:
    """Lève jwt.PyJWTError si le jeton est invalide ou expiré."""
    return jwt.decode(jeton, _cle(), algorithms=[ALGORITHME])
