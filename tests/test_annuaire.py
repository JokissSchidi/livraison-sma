"""Tests de la logique de l'annuaire (sans réseau)."""
from datetime import UTC, datetime, timedelta

from outils import pt

from livraison.db.modeles import Vehicule
from livraison.modele.annuaire import AnnuaireMemoire, EntreeAnnuaire

MAINTENANT = datetime(2026, 11, 19, 10, 0, tzinfo=UTC)


def entree(lid, position, vehicule=Vehicule.MOTO, age_s=5):
    return EntreeAnnuaire(lid, f"{lid}@localhost", vehicule, position, MAINTENANT - timedelta(seconds=age_s))


def annuaire():
    a = AnnuaireMemoire()
    a.enregistrer(entree("proche", pt(1, 0)))
    a.enregistrer(entree("moyen", pt(4, 0), Vehicule.VOITURE))
    a.enregistrer(entree("loin", pt(20, 0)))
    return a


def test_chercher_trie_par_distance_et_respecte_le_rayon():
    a = annuaire()
    assert [c.livreur_id for c in a.chercher(pt(0, 0), 5_000, maintenant=MAINTENANT)] == ["proche", "moyen"]
    assert [c.livreur_id for c in a.chercher(pt(0, 0), 50_000, maintenant=MAINTENANT)] == ["proche", "moyen", "loin"]
    assert a.chercher(pt(0, 0), 500, maintenant=MAINTENANT) == []


def test_filtre_par_vehicule():
    a = annuaire()
    trouves = a.chercher(pt(0, 0), 50_000, vehicules={Vehicule.VOITURE}, maintenant=MAINTENANT)
    assert [c.livreur_id for c in trouves] == ["moyen"]


def test_position_trop_ancienne_ignoree_puis_rafraichie():
    a = annuaire()
    a.enregistrer(entree("disparu", pt(0.5, 0), age_s=600))  # plus de nouvelles depuis 10 minutes
    assert "disparu" not in [c.livreur_id for c in a.chercher(pt(0, 0), 5_000, maintenant=MAINTENANT)]
    a.maj_position("disparu", pt(0.5, 0), MAINTENANT)
    assert "disparu" in [c.livreur_id for c in a.chercher(pt(0, 0), 5_000, maintenant=MAINTENANT)]


def test_retirer_et_position_d_un_inconnu():
    a = annuaire()
    assert a.retirer("proche") and not a.retirer("proche")
    assert len(a) == 2
    assert a.maj_position("inconnu", pt(0, 0), MAINTENANT) is False


def test_maj_position_deplace_le_livreur():
    a = annuaire()
    a.maj_position("loin", pt(0.5, 0), MAINTENANT)
    assert a.chercher(pt(0, 0), 700, maintenant=MAINTENANT)[0].livreur_id == "loin"
