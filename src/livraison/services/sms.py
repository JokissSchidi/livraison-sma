"""Envoi de SMS. Version de développement : le message s'affiche dans la console de l'API.

Pour la production, remplacer le corps de `envoyer_sms` par l'appel au prestataire choisi.
"""
import logging

journal = logging.getLogger("livraison.sms")


async def envoyer_sms(telephone: str, message: str) -> None:
    journal.warning("[SMS simulé] à %s : %s", telephone, message)
