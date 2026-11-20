"""Format des messages échangés entre agents : performatives FIPA, corps en JSON.

Tous les messages d'une attribution portent l'identifiant de la commande comme `thread`
(identifiant de conversation, cahier des charges § 3.3).
"""
from __future__ import annotations

import json
from datetime import datetime

from spade.message import Message

from livraison.db.modeles import Vehicule
from livraison.modele.offre import Demande, MotifRefus, Proposition, Refus
from livraison.services.itineraires import Coord

# Performatives
CFP = "cfp"
PROPOSE = "propose"
REFUSE = "refuse"
ACCEPT = "accept-proposal"
REJECT = "reject-proposal"
INFORM = "inform"
FAILURE = "failure"
REQUEST = "request"


def creer(vers: str, performative: str, corps: dict, thread: str | None = None) -> Message:
    msg = Message(to=vers)
    msg.set_metadata("performative", performative)
    msg.set_metadata("ontology", "livraison")
    if thread:
        msg.thread = thread
    msg.body = json.dumps(corps)
    return msg


def repondre(a: Message, performative: str, corps: dict) -> Message:
    msg = a.make_reply()
    msg.set_metadata("performative", performative)
    msg.set_metadata("ontology", "livraison")
    msg.body = json.dumps(corps)
    return msg


def lire(msg: Message) -> dict:
    return json.loads(msg.body) if msg.body else {}


def performative(msg: Message) -> str:
    return msg.get_metadata("performative") or ""


# --- conversions des objets du modèle
def coord_vers_json(c: Coord) -> dict:
    return {"lat": c.latitude, "lon": c.longitude}


def coord_depuis_json(d: dict) -> Coord:
    return Coord(float(d["lat"]), float(d["lon"])).verifier()


def demande_vers_json(d: Demande) -> dict:
    return {"enlevement": coord_vers_json(d.enlevement), "livraison": coord_vers_json(d.livraison),
            "poids_kg": d.poids_kg}


def demande_depuis_json(d: dict) -> Demande:
    poids = float(d["poids_kg"])
    if poids <= 0:
        raise ValueError("Le poids du colis doit être positif.")
    return Demande(coord_depuis_json(d["enlevement"]), coord_depuis_json(d["livraison"]), poids)


def proposition_vers_json(p: Proposition) -> dict:
    return {"livreur_id": p.livreur_id, "cout": p.cout, "approche_s": p.approche_s, "detour_s": p.detour_s,
            "penalite_s": p.penalite_s, "arrivee_estimee": p.arrivee_estimee.isoformat(), "note": p.note}


def proposition_depuis_json(d: dict) -> Proposition:
    return Proposition(d["livreur_id"], d["cout"], d["approche_s"], d["detour_s"], d["penalite_s"],
                       datetime.fromisoformat(d["arrivee_estimee"]), d.get("note"))


def refus_vers_json(r: Refus) -> dict:
    return {"livreur_id": r.livreur_id, "motif": r.motif.value, "detail": r.detail}


def refus_depuis_json(d: dict) -> Refus:
    return Refus(d["livreur_id"], MotifRefus(d["motif"]), d.get("detail", ""))


def vehicule_depuis_json(valeur: str) -> Vehicule:
    return Vehicule(valeur)
