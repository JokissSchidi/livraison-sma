"""Tests du client OSRM. Les tests « simulés » n'ont besoin d'aucun service ;
le dernier interroge le vrai OSRM (docker compose --profile itineraires up -d) et s'ignore sinon."""
import httpx
import pytest

from livraison.db.modeles import Vehicule
from livraison.services.itineraires import (
    AucunItineraire,
    ClientItineraires,
    Coord,
    ErreurItineraire,
)

PLATEAU = Coord(5.3236, -4.0197)
COCODY = Coord(5.3480, -3.9870)
URL = "http://osrm.test"


def client_simule(reponse, requetes=None) -> ClientItineraires:
    """Client dont le « serveur OSRM » est une fonction Python."""

    def gestionnaire(requete: httpx.Request) -> httpx.Response:
        if requetes is not None:
            requetes.append(requete)
        return reponse(requete) if callable(reponse) else reponse

    http = httpx.AsyncClient(transport=httpx.MockTransport(gestionnaire))
    return ClientItineraires(URL, client=http)


def route_ok(duree=600.0, distance=6700.0) -> httpx.Response:
    return httpx.Response(200, json={
        "code": "Ok",
        "routes": [{"duration": duree, "distance": distance, "legs": []}],
        "waypoints": [],
    })


async def test_trajet_voiture():
    c = client_simule(route_ok(600, 6700))
    t = await c.trajet(PLATEAU, COCODY)
    assert (t.duree_s, t.distance_m) == (600.0, 6700.0)


async def test_osrm_recoit_longitude_puis_latitude():
    requetes = []
    c = client_simule(route_ok(), requetes)
    await c.trajet(PLATEAU, COCODY)
    assert requetes[0].url.path == "/route/v1/driving/-4.019700,5.323600;-3.987000,5.348000"


async def test_moto_plus_rapide_que_voiture_et_velo_par_la_vitesse():
    c = client_simule(route_ok(1000, 6000))
    voiture = await c.trajet(PLATEAU, COCODY, Vehicule.VOITURE)
    moto = await c.trajet(PLATEAU, COCODY, Vehicule.MOTO)
    velo = await c.trajet(PLATEAU, COCODY, Vehicule.VELO)
    assert moto.duree_s == pytest.approx(0.8 * voiture.duree_s)
    assert velo.duree_s == pytest.approx(6000 / (15 / 3.6))  # 6 km à 15 km/h = 1440 s
    assert moto.distance_m == velo.distance_m == voiture.distance_m


async def test_cache_evite_une_deuxieme_requete():
    requetes = []
    c = client_simule(route_ok(), requetes)
    await c.trajet(PLATEAU, COCODY)
    await c.trajet(PLATEAU, COCODY, Vehicule.MOTO)  # même route de base
    await c.trajet(COCODY, PLATEAU)  # sens inverse : nouvelle requête
    assert len(requetes) == 2


async def test_aucun_itineraire():
    c = client_simule(httpx.Response(400, json={"code": "NoRoute", "message": "Impossible"}))
    with pytest.raises(AucunItineraire):
        await c.trajet(PLATEAU, COCODY)
    c = client_simule(httpx.Response(200, json={"code": "Ok", "routes": []}))
    with pytest.raises(AucunItineraire):
        await c.trajet(PLATEAU, COCODY)


async def test_erreurs_reseau_et_serveur():
    def en_panne(_requete):
        raise httpx.ConnectError("refusé")

    with pytest.raises(ErreurItineraire):
        await client_simule(en_panne).trajet(PLATEAU, COCODY)

    def trop_lent(_requete):
        raise httpx.ReadTimeout("lent")

    with pytest.raises(ErreurItineraire, match="à temps"):
        await client_simule(trop_lent).trajet(PLATEAU, COCODY)

    with pytest.raises(ErreurItineraire, match="illisible"):
        await client_simule(httpx.Response(502, text="Bad gateway")).trajet(PLATEAU, COCODY)

    with pytest.raises(ErreurItineraire, match="InvalidQuery"):
        await client_simule(httpx.Response(400, json={"code": "InvalidQuery"})).trajet(PLATEAU, COCODY)


async def test_coordonnees_invalides():
    c = client_simule(route_ok())
    with pytest.raises(ValueError):
        await c.trajet(Coord(95, 0), COCODY)
    with pytest.raises(ValueError):
        await c.trajet(Coord(float("nan"), 0), COCODY)


async def test_matrice_avec_une_case_sans_chemin():
    requetes = []
    reponse = httpx.Response(200, json={
        "code": "Ok",
        "durations": [[100.0], [None], [300.0]],
        "distances": [[1000.0], [None], [3000.0]],
    })
    c = client_simule(reponse, requetes)
    livreurs = [Coord(5.30, -4.00), Coord(5.31, -4.01), Coord(5.32, -4.02)]
    m = await c.matrice(livreurs, [PLATEAU], Vehicule.MOTO)
    assert m[0][0].duree_s == pytest.approx(80.0)  # 100 s voiture x 0,8
    assert m[1][0] is None
    assert m[2][0].distance_m == 3000.0
    assert len(requetes) == 1  # une seule requête pour trois livreurs
    assert requetes[0].url.params["sources"] == "0;1;2"
    assert requetes[0].url.params["destinations"] == "3"
    assert await c.matrice([], [PLATEAU]) == []


async def test_vrai_osrm_plateau_cocody():
    """Test d'intégration : s'ignore si le moteur d'itinéraires n'est pas démarré."""
    async with ClientItineraires.depuis_config() as c:
        try:
            t = await c.trajet(PLATEAU, COCODY)
        except ErreurItineraire as e:
            pytest.skip(f"OSRM injoignable : {e}")
        assert 4_000 < t.distance_m < 12_000
        assert 300 < t.duree_s < 1_800
        m = await c.matrice([PLATEAU, COCODY], [COCODY, PLATEAU])
        assert m[0][0].duree_s == pytest.approx(t.duree_s, rel=0.05)
        assert m[0][1].duree_s < 5  # un point vers lui-même
