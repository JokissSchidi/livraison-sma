"""Création des comptes XMPP des agents (un compte par agent).

Développement : commande `ejabberdctl` exécutée dans le conteneur Docker (réglage XMPP_CTL).
Production : remplacer par l'API d'administration du serveur XMPP.
"""
from __future__ import annotations

import asyncio
import shlex
import subprocess

from livraison.config import parametres


class ErreurCompteXMPP(Exception):
    pass


class ComptesEjabberd:
    def __init__(self, commande: str | None = None, domaine: str | None = None, mot_de_passe: str | None = None):
        self.commande = shlex.split(commande or parametres.xmpp_ctl)
        self.domaine = domaine or parametres.xmpp_domaine
        self.mot_de_passe = mot_de_passe or parametres.xmpp_mot_de_passe_agents
        self._connus: set[str] = set()  # comptes déjà vérifiés : évite un appel à ejabberdctl par agent

    def _executer(self, args: tuple[str, ...]) -> tuple[int, str]:
        try:
            r = subprocess.run([*self.commande, *args], capture_output=True, text=True, timeout=30)
        except FileNotFoundError as e:
            raise ErreurCompteXMPP(f"Commande introuvable : {self.commande[0]} (réglage XMPP_CTL)") from e
        except subprocess.TimeoutExpired as e:
            raise ErreurCompteXMPP("La commande ejabberdctl ne répond pas (30 s).") from e
        return r.returncode, (r.stdout + r.stderr).strip()

    async def _ctl(self, *args: str) -> tuple[int, str]:
        # Dans un fil séparé : fonctionne avec toutes les boucles asyncio (SPADE en installe une
        # particulière, sous laquelle asyncio.create_subprocess_exec n'est pas toujours disponible).
        return await asyncio.to_thread(self._executer, args)

    async def assurer(self, nom: str) -> None:
        """Crée le compte nom@domaine s'il n'existe pas déjà."""
        if nom in self._connus:
            return
        code, _ = await self._ctl("check_account", nom, self.domaine)
        if code == 0:
            self._connus.add(nom)
            return
        code, sortie = await self._ctl("register", nom, self.domaine, self.mot_de_passe)
        if code != 0 and "already registered" not in sortie:
            raise ErreurCompteXMPP(f"Création du compte {nom}@{self.domaine} impossible : {sortie}")
        self._connus.add(nom)


class ComptesDejaCrees:
    """Pour les tests ou un serveur où les comptes existent déjà."""

    async def assurer(self, nom: str) -> None:
        return None
