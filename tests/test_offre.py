"""Tests du calcul du coût d'une offre (aucun service nécessaire : moteur d'itinéraires simulé)."""
import math
from datetime import UTC, datetime, timedelta

import pytest

from livraison.db.modeles import Vehicule
from livraison.modele.offre import (
    Coefficients,
    Demande,
    MotifRefus,
    Proposition,
    Refus,
    SituationLivreur,
    Zone,
    classer,
    distance_m,
    evaluer_offre,
)
from livraison.services.itineraires import AucunItineraire, Coord, ErreurItineraire, Trajet

MAINTENANT = datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)
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


def livreur(**kw) -> SituationLivreur:
    base = dict(
        livreur_id="L1", vehicule=Vehicule.MOTO, capacite_kg=30.0, position=pt(0, 0),
        position_le=MAINTENANT - timedelta(seconds=10),
    )
    return SituationLivreur(**{**base, **kw})


COLIS = Demande(enlevement=pt(5, 0), livraison=pt(5, 5), poids_kg=2.0)
COEF = Coefficients()


async def evaluer(situation, demande=COLIS, coef=COEF, moteur=None):
    return await evaluer_offre(demande, situation, coef, moteur or FauxMoteur(), MAINTENANT)


# ----------------------------------------------------------------------------- offres normales

async def test_livreur_libre_cout_egal_a_l_approche():
    p = await evaluer(livreur())
    assert isinstance(p, Proposition)
    assert p.approche_s == pytest.approx(500, rel=0.01)  # 5 km à 36 km/h
    assert p.detour_s == pytest.approx(0, abs=1)
    assert p.penalite_s == 0
    assert p.cout == pytest.approx(500, rel=0.01)
    assert p.arrivee_estimee == MAINTENANT + timedelta(seconds=p.approche_s)


async def test_colis_sur_le_chemin_coute_moins_qu_un_livreur_libre():
    # Le livreur monte vers le nord (arrêt à 10 km) ; le colis est entièrement sur sa route.
    colis = Demande(enlevement=pt(0, 5), livraison=pt(0, 8), poids_kg=2.0)
    occupe = await evaluer(livreur(arrets_restants=(pt(0, 10),), nb_courses=1, charge_kg=3), colis)
    libre = await evaluer(livreur(livreur_id="L2"), colis)
    assert occupe.detour_s < 0  # le colis est « sur le chemin »
    assert occupe.cout == pytest.approx(180, abs=2)  # seule la pénalité de charge reste
    assert occupe.cout < libre.cout  # regrouper les courses est avantageux


async def test_detour_grand_si_le_colis_est_a_l_oppose():
    arrets = (pt(0, 10),)
    sur_la_route = Demande(enlevement=pt(0, 5), livraison=pt(0, 8), poids_kg=2.0)
    a_l_oppose = Demande(enlevement=pt(0, -8), livraison=pt(0, -5), poids_kg=2.0)
    a = await evaluer(livreur(arrets_restants=arrets, nb_courses=1), sur_la_route)
    b = await evaluer(livreur(arrets_restants=arrets, nb_courses=1), a_l_oppose)
    # Colis au sud : 8 km d'approche + 3 km de course + 15 km pour rejoindre l'arrêt, au lieu de 10 km.
    assert b.detour_s == pytest.approx(500, rel=0.03)
    assert b.detour_s > a.detour_s + 500


async def test_penalite_proportionnelle_a_la_charge():
    sans = await evaluer(livreur())
    deux = await evaluer(livreur(arrets_restants=(pt(-3, 0), pt(-3, 2)), nb_courses=2, charge_kg=4))
    assert deux.penalite_s == 2 * COEF.penalite_charge_s
    assert deux.cout > sans.cout


async def test_cout_jamais_negatif():
    colis = Demande(enlevement=pt(0, 5), livraison=pt(0, 8), poids_kg=2.0)
    p = await evaluer(livreur(arrets_restants=(pt(0, 10),), nb_courses=1), colis,
                      coef=Coefficients(penalite_charge_s=0))
    assert p.cout == 0.0


async def test_une_seule_requete_de_matrice_avec_le_bon_vehicule():
    moteur = FauxMoteur()
    await evaluer(livreur(vehicule=Vehicule.VELO, arrets_restants=(pt(1, 1), pt(2, 2)), nb_courses=1), moteur=moteur)
    assert moteur.appels == [(5, Vehicule.VELO)]  # position + 2 arrêts + enlèvement + livraison


# ----------------------------------------------------------------------------- refus motivés

async def test_refus_sature():
    r = await evaluer(livreur(nb_courses=3, arrets_restants=(pt(1, 1),)))
    assert isinstance(r, Refus) and r.motif == MotifRefus.SATURE


async def test_refus_colis_trop_lourd():
    r = await evaluer(livreur(capacite_kg=1.0))
    assert r.motif == MotifRefus.TROP_LOURD
    # Le colis passe seul, mais pas en plus de ce qui est déjà à bord.
    r = await evaluer(livreur(capacite_kg=10.0, charge_kg=9.0, nb_courses=1, arrets_restants=(pt(1, 1),)))
    assert r.motif == MotifRefus.TROP_LOURD


async def test_refus_position_gps_trop_ancienne():
    r = await evaluer(livreur(position_le=MAINTENANT - timedelta(minutes=10)))
    assert r.motif == MotifRefus.GPS_PERIME


async def test_refus_hors_zone():
    zone = Zone(centre=pt(0, 0), rayon_m=6_000)
    coef = Coefficients(zone=zone)
    dedans = Demande(enlevement=pt(1, 1), livraison=pt(2, 2), poids_kg=1)
    assert isinstance(await evaluer(livreur(), dedans, coef), Proposition)
    assert (await evaluer(livreur(), COLIS, coef)).motif == MotifRefus.HORS_ZONE  # livraison à 7 km


async def test_refus_si_moteur_en_panne_ou_point_inaccessible():
    r = await evaluer(livreur(), moteur=FauxMoteur(panne=ErreurItineraire("OSRM arrêté")))
    assert r.motif == MotifRefus.MOTEUR_INDISPONIBLE
    r = await evaluer(livreur(), moteur=FauxMoteur(panne=AucunItineraire("pas de route")))
    assert r.motif == MotifRefus.AUCUN_ITINERAIRE
    r = await evaluer(livreur(), moteur=FauxMoteur(sans_chemin={COLIS.enlevement}))
    assert r.motif == MotifRefus.AUCUN_ITINERAIRE


async def test_les_refus_rapides_n_interrogent_pas_le_moteur():
    moteur = FauxMoteur()
    await evaluer(livreur(nb_courses=3), moteur=moteur)
    await evaluer(livreur(capacite_kg=0.5), moteur=moteur)
    assert moteur.appels == []


# ----------------------------------------------------------------------------- classement

def prop(lid, cout, note=None, minutes=0) -> Proposition:
    return Proposition(lid, cout, cout, 0, 0, MAINTENANT + timedelta(minutes=minutes), note)


def test_classement_par_cout_puis_note_puis_arrivee():
    offres = [prop("lent", 300, 5.0), prop("a", 100, 4.0), prop("b", 100.5, 4.8), prop("c", 100.2, None, 1)]
    ordre = [p.livreur_id for p in classer(offres, COEF)]
    assert ordre == ["b", "a", "c", "lent"]  # a, b, c sont « égaux » (< 1 s) : la meilleure note passe d'abord
    assert classer([], COEF) == []


def test_classement_cout_strictement_meilleur_gagne():
    offres = [prop("x", 200, 5.0), prop("y", 150, 3.0)]
    assert classer(offres, COEF)[0].livreur_id == "y"  # la note ne compense jamais un vrai écart de coût


def test_distance_haversine():
    assert distance_m(pt(0, 0), pt(3, 4)) == pytest.approx(5_000, rel=0.01)
    assert distance_m(pt(1, 1), pt(1, 1)) == 0
    assert not math.isnan(distance_m(Coord(0, 0), Coord(0, 180)))
