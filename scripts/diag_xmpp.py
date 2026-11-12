"""Diagnostic de la connexion XMPP : test TCP brut, puis une seule tentative SPADE détaillée."""
import asyncio
import logging
import socket

from livraison.config import parametres

HOTE = parametres.xmpp_domaine
PORT = parametres.xmpp_port


def test_tcp():
    print(f"--- Test TCP vers {HOTE}:{PORT}")
    try:
        infos = socket.getaddrinfo(HOTE, PORT, type=socket.SOCK_STREAM)
        print("Adresses résolues :", [i[4][0] for i in infos])
        s = socket.create_connection((HOTE, PORT), timeout=5)
        print("Connecté à", s.getpeername())
        s.sendall(
            f"<stream:stream to='{HOTE}' xmlns='jabber:client' "
            "xmlns:stream='http://etherx.jabber.org/streams' version='1.0'>".encode()
        )
        s.settimeout(5)
        print("Réponse du serveur :", s.recv(3000).decode(errors="replace")[:600])
        s.close()
    except Exception as e:
        print("ÉCHEC TCP :", repr(e))


async def test_spade():
    from spade.agent import Agent

    class Test(Agent):
        async def setup(self):
            pass

    print("--- Test SPADE (une seule tentative)")
    agent = Test(parametres.jid("pong"), parametres.xmpp_mot_de_passe_agents, port=PORT)
    try:
        await agent.start(auto_register=False)
        print("CONNECTÉ")
        await agent.stop()
    except Exception as e:
        print("ÉCHEC SPADE :", repr(e))


if __name__ == "__main__":
    test_tcp()
    logging.basicConfig(level=logging.DEBUG)
    asyncio.run(test_spade(), loop_factory=asyncio.SelectorEventLoop)