"""Crée un compte administrateur ou répartiteur (non inscriptibles depuis l'API).

Exemple : python scripts/creer_utilisateur.py --telephone +2250700000000 --nom "Jokiss" --role administrateur
"""
import argparse
import asyncio
import re
import sys

from sqlalchemy import select

from livraison.db.base import fabrique_session, moteur
from livraison.db.modeles import Role, Utilisateur


async def main(telephone: str, nom: str, role: Role) -> int:
    telephone = re.sub(r"[\s.\-()]", "", telephone)
    if not re.match(r"^\+\d{8,15}$", telephone):
        print("Numéro invalide : utilisez le format international, par ex. +2250700000000.")
        return 1
    async with fabrique_session() as session:
        if await session.scalar(select(Utilisateur).where(Utilisateur.telephone == telephone)):
            print(f"Le numéro {telephone} existe déjà.")
            return 1
        session.add(Utilisateur(telephone=telephone, nom=nom, role=role))
        await session.commit()
    await moteur.dispose()
    print(f"Compte {role.value} créé pour {nom} ({telephone}).")
    return 0


if __name__ == "__main__":
    analyseur = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    analyseur.add_argument("--telephone", required=True)
    analyseur.add_argument("--nom", required=True)
    analyseur.add_argument("--role", choices=[Role.ADMINISTRATEUR.value, Role.REPARTITEUR.value],
                           default=Role.ADMINISTRATEUR.value)
    a = analyseur.parse_args()
    sys.exit(asyncio.run(main(a.telephone, a.nom, Role(a.role))))
