"""Agent Annuaire : sait quels livreurs sont en ligne et où (cahier des charges § 3.1)."""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from spade.agent import Agent
from spade.behaviour import CyclicBehaviour
from spade.template import Template

from livraison.agents import messages as m
from livraison.db.modeles import Vehicule
from livraison.modele.annuaire import AnnuaireMemoire, EntreeAnnuaire

journal = logging.getLogger("livraison.annuaire")


class AnnuaireAgent(Agent):
    """Actions (performative « request », corps JSON {"action": ...}) :

    - enregistrer : un livreur passe en ligne ;
    - retirer     : il se déconnecte ;
    - position    : nouvelle position GPS ;
    - chercher    : le Dispatcher demande les livreurs proches d'un point (réponse « inform »).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.annuaire = AnnuaireMemoire()

    class Ecouter(CyclicBehaviour):
        async def run(self):
            msg = await self.receive(timeout=5)
            if msg is None:
                return
            try:
                await self.traiter(msg)
            except (KeyError, ValueError, TypeError) as e:
                journal.warning("Message invalide ignoré (%s) : %s", type(e).__name__, e)
                await self.send(m.repondre(msg, m.FAILURE, {"erreur": str(e)}))

        async def traiter(self, msg):
            corps = m.lire(msg)
            action = corps["action"]
            annuaire: AnnuaireMemoire = self.agent.annuaire
            if action == "enregistrer":
                annuaire.enregistrer(EntreeAnnuaire(
                    livreur_id=corps["livreur_id"], jid=str(msg.sender.bare),
                    vehicule=Vehicule(corps["vehicule"]),
                    position=m.coord_depuis_json(corps["position"]), mis_a_jour_le=datetime.now(UTC)))
            elif action == "retirer":
                annuaire.retirer(corps["livreur_id"])
            elif action == "position":
                annuaire.maj_position(corps["livreur_id"], m.coord_depuis_json(corps["position"]),
                                      datetime.now(UTC))
            elif action == "chercher":
                vehicules = {Vehicule(v) for v in corps.get("vehicules", [])} or None
                trouves = annuaire.chercher(
                    m.coord_depuis_json(corps["centre"]), float(corps["rayon_m"]),
                    vehicules=vehicules, age_max_s=float(corps.get("age_max_s", 120)))
                await self.send(m.repondre(msg, m.INFORM, {"livreurs": [
                    {"livreur_id": c.livreur_id, "jid": c.jid, "vehicule": c.vehicule.value,
                     "distance_m": c.distance_m} for c in trouves]}))
            else:
                raise ValueError(f"action inconnue : {action}")

    async def setup(self):
        modele = Template()
        modele.set_metadata("performative", m.REQUEST)
        self.add_behaviour(self.Ecouter(), modele)
