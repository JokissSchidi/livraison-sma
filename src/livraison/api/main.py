"""Serveur applicatif. Lancement : uvicorn livraison.api.main:app --reload"""
from fastapi import FastAPI

from livraison.api import admin, auth

app = FastAPI(title="Livraison SMA", version="0.2.0")
app.include_router(auth.routeur)
app.include_router(admin.routeur)


@app.get("/sante")
async def sante() -> dict:
    """Point de contrôle utilisé pour vérifier que le serveur répond."""
    return {"etat": "ok"}
