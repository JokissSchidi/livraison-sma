"""Outils communs aux tests : points sur un plan et moteur d'itinéraires simulé."""
from livraison.db.modeles import Vehicule
from livraison.modele.offre import distance_m
from livraison.services.itineraires import Coord, Trajet

VITESSE_MS = 10.0  # 36 km/h


def pt(x_km: float, y_km: float) -> Coord:
    """Point d'un plan centré sur Abidjan : x vers l'est, y vers le nord (en km)."""
    return Coord(5.30 + y_km / 111.0, -4.00 + x_km / 110.5)


class FauxMoteur:
    """Durée = distance à vol d'oiseau / 36 km/h. Permet de choisir les pannes."""

    def __init__(self, panne: Exception | None = None, sans_chemin: set[Coord] | None = None):
        self.appels: list[tuple[int, Vehicule]] = []
        self.panne = panne
        self.sans_chemin = sans_chemin or set()

    async def matrice(self, sources, destinations, vehicule=Vehicule.VOITURE):
        self.appels.append((len(sources), vehicule))
        if self.panne:
            raise self.panne
        resultat = []
        for a in sources:
            ligne = []
            for b in destinations:
                if a in self.sans_chemin or b in self.sans_chemin:
                    ligne.append(None)
                else:
                    m = distance_m(a, b)
                    ligne.append(Trajet(m / VITESSE_MS, m))
            resultat.append(ligne)
        return resultat
