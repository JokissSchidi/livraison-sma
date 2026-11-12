"""Test de la plateforme multi-agents : équivalent SPADE des agents Ping et Pong du TP.

Prérequis : le serveur XMPP tourne dans un autre terminal (commande : spade run).
Lancement : python -m livraison.agents.ping_pong
"""
import asyncio

from spade.agent import Agent, DisconnectedException
from spade.behaviour import CyclicBehaviour, PeriodicBehaviour
from spade.message import Message
from spade.template import Template

from livraison.config import parametres

NB_PINGS = 3
TENTATIVES = 5


class Pong(Agent):
    class Repondre(CyclicBehaviour):
        async def run(self):
            msg = await self.receive(timeout=10)
            if msg:
                print(f"pong a reçu « {msg.body} » de {msg.sender.local}")
                reponse = msg.make_reply()
                reponse.set_metadata("performative", "inform")
                reponse.body = msg.body.replace("ping", "pong")
                await self.send(reponse)

    async def setup(self):
        modele = Template()
        modele.set_metadata("performative", "request")
        self.add_behaviour(self.Repondre(), modele)


class Ping(Agent):
    class Emettre(PeriodicBehaviour):
        async def on_start(self):
            self.envoyes = 0

        async def run(self):
            self.envoyes += 1
            msg = Message(to=parametres.jid("pong"))
            msg.set_metadata("performative", "request")
            msg.thread = "test-ping"
            msg.body = f"ping #{self.envoyes}"
            await self.send(msg)
            print(f"ping -> {msg.body}")
            if self.envoyes >= NB_PINGS:
                self.kill()

    class Recevoir(CyclicBehaviour):
        async def on_start(self):
            self.recus = 0

        async def run(self):
            msg = await self.receive(timeout=10)
            if msg:
                self.recus += 1
                print(f"ping <- « {msg.body} »")
                if self.recus >= NB_PINGS:
                    self.agent.termine.set()

    async def setup(self):
        self.termine = asyncio.Event()
        modele = Template()
        modele.set_metadata("performative", "inform")
        self.add_behaviour(self.Recevoir(), modele)
        self.add_behaviour(self.Emettre(period=1))


async def demarrer(classe, nom):
    """Démarre un agent ; réessaie si la connexion est coupée pendant la négociation."""
    for essai in range(1, TENTATIVES + 1):
        agent = classe(
            parametres.jid(nom),
            parametres.xmpp_mot_de_passe_agents,
            port=parametres.xmpp_port,
        )
        try:
            await agent.start(auto_register=True)
            print(f"[{nom}] connecté (essai {essai})")
            return agent
        except DisconnectedException:
            print(f"[{nom}] connexion interrompue (essai {essai}/{TENTATIVES})")
            try:
                await agent.stop()
            except Exception:
                pass
            await asyncio.sleep(1)
    raise RuntimeError(f"Impossible de connecter l'agent {nom} au serveur XMPP.")


async def main() -> bool:
    pong = await demarrer(Pong, "pong")
    ping = await demarrer(Ping, "ping")
    try:
        await asyncio.wait_for(ping.termine.wait(), timeout=20)
        print("OK : les agents communiquent.")
        return True
    except TimeoutError:
        print("ÉCHEC : aucune réponse de pong dans le délai.")
        return False
    finally:
        await ping.stop()
        await pong.stop()


if __name__ == "__main__":
    resultat = []

    async def lancer():
        resultat.append(await main())

    asyncio.run(lancer(), loop_factory=asyncio.SelectorEventLoop)
    raise SystemExit(0 if resultat and resultat[0] else 1)