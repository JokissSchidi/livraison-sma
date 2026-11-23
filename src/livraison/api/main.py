"""Serveur applicatif. Lancement : uvicorn livraison.api.main:app --reload"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from livraison.agents.plateforme import Plateforme
from livraison.api import admin, auth, commandes, livreurs
from livraison.db.base import fabrique_session

journal = logging.getLogger("livraison.api")


@asynccontextmanager
async def cycle_de_vie(app: FastAPI):
    """Démarre les agents avec l'API. Si le serveur XMPP est arrêté, l'API démarre quand même (503 sur les routes d'attribution)."""
    plateforme = Plateforme(fabrique_session)
    app.state.plateforme = None
    try:
        await plateforme.demarrer()
        app.state.plateforme = plateforme
    except Exception:  # noqa: BLE001
        journal.exception("Plateforme multi-agents indisponible : l'API démarre sans elle (XMPP arrêté ?)")
        try:
            await plateforme.arreter()
        except Exception:  # noqa: BLE001
            pass
    yield
    if app.state.plateforme is not None:
        await plateforme.arreter()


app = FastAPI(title="Livraison SMA", version="0.3.0", lifespan=cycle_de_vie)
app.include_router(auth.routeur)
app.include_router(admin.routeur)
app.include_router(commandes.routeur)
app.include_router(livreurs.routeur)


@app.get("/sante")
async def sante() -> dict:
    """Point de contrôle utilisé pour vérifier que le serveur répond."""
    return {"etat": "ok", "agents": getattr(app.state, "plateforme", None) is not None}
