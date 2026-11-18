"""Tests de l'authentification et des droits (nécessitent PostgreSQL : voir conftest.py)."""
import re
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from livraison.api import auth as module_auth
from livraison.api.main import app
from livraison.config import parametres
from livraison.db.base import obtenir_session
from livraison.db.modeles import CodeVerification, Role, Utilisateur
from livraison.services.securite import creer_jeton

TEL = "+2250700000010"


@pytest_asyncio.fixture
async def api(session, monkeypatch):
    """Client HTTP branché sur la session annulable ; les SMS sont capturés."""
    monkeypatch.setattr(parametres, "jwt_secret", "x" * 40)
    sms: list[str] = []

    async def faux_envoi(telephone: str, message: str) -> None:
        sms.append(message)

    monkeypatch.setattr(module_auth, "envoyer_sms", faux_envoi)

    async def session_de_test():
        yield session

    app.dependency_overrides[obtenir_session] = session_de_test
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        client.sms = sms
        yield client
    app.dependency_overrides.clear()


def code_recu(api) -> str:
    return re.search(r"\b(\d{6})\b", api.sms[-1]).group(1)


async def autoriser_renvoi(session):
    """Fait « vieillir » les codes pour passer outre la limite d'un envoi par 30 s."""
    await session.execute(update(CodeVerification).values(cree_le=datetime.now(UTC) - timedelta(minutes=1)))


async def connecter(api, session, telephone=TEL) -> str:
    await autoriser_renvoi(session)
    await api.post("/auth/code", json={"telephone": telephone})
    r = await api.post("/auth/verifier", json={"telephone": telephone, "code": code_recu(api)})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def test_inscription_client_puis_connexion(api):
    r = await api.post("/auth/inscription", json={"telephone": "+225 07 00 00 00 10", "nom": "Awa", "role": "client"})
    assert r.status_code == 201
    assert len(api.sms) == 1 and "code" in api.sms[0]
    r = await api.post("/auth/verifier", json={"telephone": TEL, "code": code_recu(api)})
    assert r.status_code == 200
    jeton = r.json()["access_token"]
    moi = await api.get("/auth/moi", headers={"Authorization": f"Bearer {jeton}"})
    assert moi.status_code == 200
    assert moi.json()["telephone"] == TEL and moi.json()["role"] == "client"


async def test_code_a_usage_unique(api):
    await api.post("/auth/inscription", json={"telephone": TEL, "nom": "Awa", "role": "client"})
    code = code_recu(api)
    assert (await api.post("/auth/verifier", json={"telephone": TEL, "code": code})).status_code == 200
    assert (await api.post("/auth/verifier", json={"telephone": TEL, "code": code})).status_code == 400


async def test_mauvais_codes_bloquent_apres_cinq_essais(api):
    await api.post("/auth/inscription", json={"telephone": TEL, "nom": "Awa", "role": "client"})
    bon = code_recu(api)
    faux = "000000" if bon != "000000" else "111111"
    for _ in range(5):
        assert (await api.post("/auth/verifier", json={"telephone": TEL, "code": faux})).status_code == 400
    r = await api.post("/auth/verifier", json={"telephone": TEL, "code": bon})
    assert r.status_code == 429


async def test_code_expire(api, session):
    await api.post("/auth/inscription", json={"telephone": TEL, "nom": "Awa", "role": "client"})
    await session.execute(update(CodeVerification).values(expire_le=datetime.now(UTC) - timedelta(seconds=1)))
    r = await api.post("/auth/verifier", json={"telephone": TEL, "code": code_recu(api)})
    assert r.status_code == 400


async def test_renvoi_trop_rapide(api):
    await api.post("/auth/inscription", json={"telephone": TEL, "nom": "Awa", "role": "client"})
    r = await api.post("/auth/code", json={"telephone": TEL})
    assert r.status_code == 429


async def test_numero_inconnu_meme_reponse_sans_sms(api):
    r = await api.post("/auth/code", json={"telephone": "+2250799999999"})
    assert r.status_code == 202 and api.sms == []


async def test_inscription_refusee(api):
    base = {"telephone": TEL, "nom": "X"}
    assert (await api.post("/auth/inscription", json={**base, "role": "administrateur"})).status_code == 422
    assert (await api.post("/auth/inscription", json={**base, "role": "livreur"})).status_code == 422
    assert (await api.post("/auth/inscription", json={"telephone": "abc", "nom": "X", "role": "client"})).status_code == 422
    await api.post("/auth/inscription", json={**base, "role": "client"})
    assert (await api.post("/auth/inscription", json={**base, "role": "client"})).status_code == 409


async def test_acces_sans_jeton_ou_jeton_invalide(api):
    assert (await api.get("/auth/moi")).status_code == 401
    assert (await api.get("/auth/moi", headers={"Authorization": "Bearer nimportequoi"})).status_code == 401


async def test_validation_livreur_par_administrateur(api, session):
    # Le livreur s'inscrit : dossier « en attente ».
    r = await api.post("/auth/inscription", json={
        "telephone": TEL, "nom": "Koffi", "role": "livreur", "vehicule": "moto", "capacite_kg": 25})
    assert r.status_code == 201
    jeton_livreur = await connecter(api, session)
    moi = await api.get("/auth/moi", headers={"Authorization": f"Bearer {jeton_livreur}"})
    assert moi.json()["validation_livreur"] == "en_attente"
    livreur_id = moi.json()["id"]

    # Un livreur ne peut pas accéder à l'administration.
    r = await api.get("/admin/livreurs", headers={"Authorization": f"Bearer {jeton_livreur}"})
    assert r.status_code == 403

    # Un administrateur voit le dossier et le valide.
    admin = Utilisateur(telephone="+2250700000099", nom="Admin", role=Role.ADMINISTRATEUR)
    session.add(admin)
    await session.flush()
    entete = {"Authorization": f"Bearer {creer_jeton(admin.id, 'administrateur')}"}
    r = await api.get("/admin/livreurs", headers=entete)
    assert [d["utilisateur_id"] for d in r.json()] == [livreur_id]
    r = await api.post(f"/admin/livreurs/{livreur_id}/valider", headers=entete)
    assert r.json()["validation"] == "valide"
    moi = await api.get("/auth/moi", headers={"Authorization": f"Bearer {jeton_livreur}"})
    assert moi.json()["validation_livreur"] == "valide"
    assert (await api.get("/admin/livreurs", headers=entete)).json() == []


async def test_code_jamais_stocke_en_clair(api, session):
    await api.post("/auth/inscription", json={"telephone": TEL, "nom": "Awa", "role": "client"})
    stocke = await session.scalar(select(CodeVerification.code_hash).where(CodeVerification.telephone == TEL))
    assert code_recu(api) not in stocke and len(stocke) == 64
