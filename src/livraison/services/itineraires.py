"""Client du moteur d'itinéraires OSRM : durées et distances réelles entre des points GPS.

Usage typique (depuis un agent ou l'API) :

    itineraires = ClientItineraires.depuis_config()
    trajet = await itineraires.trajet(Coord(5.3236, -4.0197), Coord(5.3480, -3.9870), Vehicule.MOTO)
    print(trajet.duree_s, trajet.distance_m)

OSRM attend les coordonnées dans l'ordre longitude, latitude : cette conversion est faite ici,
le reste du projet manipule toujours des `Coord(latitude, longitude)`.
"""
from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass
from typing import NamedTuple

import httpx

from livraison.config import parametres
from livraison.db.modeles import Vehicule


class ErreurItineraire(Exception):
    """Le moteur d'itinéraires est injoignable ou a répondu par une erreur."""


class AucunItineraire(ErreurItineraire):
    """Aucun chemin routier entre les deux points (ou point trop éloigné d'une route)."""


class Coord(NamedTuple):
    latitude: float
    longitude: float

    def verifier(self) -> "Coord":
        if not (math.isfinite(self.latitude) and math.isfinite(self.longitude)):
            raise ValueError(f"Coordonnées invalides : {self}")
        if not (-90 <= self.latitude <= 90 and -180 <= self.longitude <= 180):
            raise ValueError(f"Coordonnées hors limites : {self}")
        return self

    def pour_osrm(self) -> str:
        return f"{self.longitude:.6f},{self.latitude:.6f}"


@dataclass(frozen=True)
class Trajet:
    duree_s: float
    distance_m: float


@dataclass(frozen=True)
class ProfilVehicule:
    """Adapte un trajet « voiture » à un autre véhicule.

    - facteur_duree : multiplie la durée (moto plus rapide que la voiture dans la circulation) ;
    - vitesse_kmh   : si défini, durée = distance / vitesse (vélo) ;
    - url           : adresse d'une instance OSRM dédiée à ce véhicule (profil moto ou vélo réel).
    """

    facteur_duree: float = 1.0
    vitesse_kmh: float | None = None
    url: str | None = None


# Approximations provisoires, à remplacer par des profils OSRM dédiés puis à calibrer
# avec des courses réelles (lot 7).
PROFILS_PAR_DEFAUT: dict[Vehicule, ProfilVehicule] = {
    Vehicule.VOITURE: ProfilVehicule(),
    Vehicule.MOTO: ProfilVehicule(facteur_duree=0.8),
    Vehicule.VELO: ProfilVehicule(vitesse_kmh=15.0),
}


class ClientItineraires:
    def __init__(
        self,
        url: str,
        *,
        profils: dict[Vehicule, ProfilVehicule] | None = None,
        client: httpx.AsyncClient | None = None,
        delai_s: float = 5.0,
        taille_cache: int = 2048,
    ) -> None:
        self.url = url.rstrip("/")
        self.profils = {**PROFILS_PAR_DEFAUT, **(profils or {})}
        self._client = client or httpx.AsyncClient(timeout=delai_s)
        self._possede_client = client is None
        self._cache: OrderedDict[tuple, Trajet] = OrderedDict()
        self._taille_cache = taille_cache

    @classmethod
    def depuis_config(cls) -> "ClientItineraires":
        return cls(parametres.osrm_url)

    async def fermer(self) -> None:
        if self._possede_client:
            await self._client.aclose()

    async def __aenter__(self) -> "ClientItineraires":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.fermer()

    # ------------------------------------------------------------------ API publique

    async def trajet(
        self, origine: Coord, destination: Coord, vehicule: Vehicule = Vehicule.VOITURE
    ) -> Trajet:
        """Durée (s) et distance (m) du meilleur trajet. Lève AucunItineraire ou ErreurItineraire."""
        origine.verifier()
        destination.verifier()
        profil = self._profil(vehicule)
        url = profil.url or self.url
        cle = (url, self._arrondi(origine), self._arrondi(destination))
        base = self._cache.get(cle)
        if base is None:
            donnees = await self._requete(
                f"{url}/route/v1/driving/{origine.pour_osrm()};{destination.pour_osrm()}",
                "overview=false",
            )
            routes = donnees.get("routes") or []
            if not routes:
                raise AucunItineraire("Aucun itinéraire entre ces deux points.")
            base = Trajet(float(routes[0]["duration"]), float(routes[0]["distance"]))
            self._memoriser(cle, base)
        else:
            self._cache.move_to_end(cle)
        return self._adapter(base, profil)

    async def matrice(
        self,
        sources: list[Coord],
        destinations: list[Coord],
        vehicule: Vehicule = Vehicule.VOITURE,
    ) -> list[list[Trajet | None]]:
        """Trajets de chaque source vers chaque destination, en une seule requête.

        Une case vaut None quand aucun chemin n'existe. Utile pour comparer d'un coup
        plusieurs livreurs au même point d'enlèvement.
        """
        if not sources or not destinations:
            return []
        for point in (*sources, *destinations):
            point.verifier()
        profil = self._profil(vehicule)
        url = profil.url or self.url
        coords = ";".join(p.pour_osrm() for p in (*sources, *destinations))
        ns = len(sources)
        donnees = await self._requete(
            f"{url}/table/v1/driving/{coords}",
            "sources=" + ";".join(str(i) for i in range(ns))
            + "&destinations=" + ";".join(str(ns + j) for j in range(len(destinations)))
            + "&annotations=duration,distance",
        )
        durees, distances = donnees["durations"], donnees["distances"]
        resultat: list[list[Trajet | None]] = []
        for ligne_d, ligne_m in zip(durees, distances, strict=True):
            ligne: list[Trajet | None] = []
            for d, m in zip(ligne_d, ligne_m, strict=True):
                ligne.append(None if d is None or m is None else self._adapter(Trajet(float(d), float(m)), profil))
            resultat.append(ligne)
        return resultat

    # ------------------------------------------------------------------ interne

    def _profil(self, vehicule: Vehicule) -> ProfilVehicule:
        try:
            return self.profils[vehicule]
        except KeyError:
            raise ValueError(f"Véhicule inconnu : {vehicule!r}") from None

    @staticmethod
    def _adapter(base: Trajet, profil: ProfilVehicule) -> Trajet:
        if profil.vitesse_kmh:
            return Trajet(base.distance_m / (profil.vitesse_kmh / 3.6), base.distance_m)
        return Trajet(base.duree_s * profil.facteur_duree, base.distance_m)

    @staticmethod
    def _arrondi(p: Coord) -> tuple[float, float]:
        return (round(p.latitude, 5), round(p.longitude, 5))  # environ 1 m

    def _memoriser(self, cle: tuple, trajet: Trajet) -> None:
        self._cache[cle] = trajet
        while len(self._cache) > self._taille_cache:
            self._cache.popitem(last=False)

    async def _requete(self, url: str, requete: str) -> dict:
        # La requête est envoyée telle quelle (« ; » et « , » non encodés), comme dans la doc d'OSRM.
        try:
            reponse = await self._client.get(f"{url}?{requete}")
        except httpx.TimeoutException as e:
            raise ErreurItineraire("Le moteur d'itinéraires ne répond pas à temps.") from e
        except httpx.HTTPError as e:
            raise ErreurItineraire(f"Moteur d'itinéraires injoignable ({type(e).__name__}).") from e
        try:
            donnees = reponse.json()
        except ValueError as e:
            raise ErreurItineraire(f"Réponse illisible du moteur d'itinéraires (HTTP {reponse.status_code}).") from e
        code = donnees.get("code")
        if code == "Ok":
            return donnees
        if code in ("NoRoute", "NoSegment"):
            raise AucunItineraire(donnees.get("message") or "Aucun itinéraire entre ces deux points.")
        raise ErreurItineraire(f"Erreur du moteur d'itinéraires : {code} {donnees.get('message', '')}".strip())
