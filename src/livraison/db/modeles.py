"""Modèle de données (cahier des charges, section 7.1)."""
import enum
import uuid
from datetime import datetime

from geoalchemy2 import Geography
from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from livraison.db.base import Base


class Role(enum.StrEnum):
    CLIENT = "client"
    LIVREUR = "livreur"
    REPARTITEUR = "repartiteur"
    ADMINISTRATEUR = "administrateur"


class EtatCompte(enum.StrEnum):
    ACTIF = "actif"
    SUSPENDU = "suspendu"


class Vehicule(enum.StrEnum):
    VELO = "velo"
    MOTO = "moto"
    VOITURE = "voiture"


class ValidationLivreur(enum.StrEnum):
    EN_ATTENTE = "en_attente"
    VALIDE = "valide"
    REFUSE = "refuse"


class EtatCommande(enum.StrEnum):
    """Cycle de vie d'une commande (section 3.4)."""

    CREEE = "creee"
    VALIDEE = "validee"  # payée, ou validée pour un paiement à la livraison
    EN_RECHERCHE = "en_recherche"
    ATTRIBUEE = "attribuee"
    ENLEVEE = "enlevee"
    EN_LIVRAISON = "en_livraison"
    LIVREE = "livree"
    CLOTUREE = "cloturee"
    ANNULEE = "annulee"
    ECHEC_LIVRAISON = "echec_livraison"
    LITIGE = "litige"


class ResultatOffre(enum.StrEnum):
    PROPOSEE = "proposee"
    RETENUE = "retenue"
    PERDUE = "perdue"
    REFUSEE = "refusee"


class ModePaiement(enum.StrEnum):
    ESPECES = "especes"
    MOBILE_MONEY = "mobile_money"
    CARTE = "carte"


class EtatPaiement(enum.StrEnum):
    EN_ATTENTE = "en_attente"
    PAYE = "paye"
    ECHEC = "echec"
    REMBOURSE = "rembourse"


def enum_texte(classe: type[enum.StrEnum]) -> Enum:
    """Énumération stockée en texte (pas de type natif PostgreSQL : migrations plus simples)."""
    return Enum(
        classe,
        native_enum=False,
        length=20,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


def point() -> Geography:
    return Geography(geometry_type="POINT", srid=4326)


class Utilisateur(Base):
    __tablename__ = "utilisateurs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    telephone: Mapped[str] = mapped_column(String(20), unique=True)
    nom: Mapped[str] = mapped_column(String(120))
    role: Mapped[Role] = mapped_column(enum_texte(Role))
    etat: Mapped[EtatCompte] = mapped_column(enum_texte(EtatCompte), default=EtatCompte.ACTIF)
    cree_le: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    livreur: Mapped["Livreur | None"] = relationship(back_populates="utilisateur")


class Livreur(Base):
    __tablename__ = "livreurs"

    utilisateur_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("utilisateurs.id", ondelete="CASCADE"), primary_key=True
    )
    vehicule: Mapped[Vehicule] = mapped_column(enum_texte(Vehicule))
    capacite_kg: Mapped[float] = mapped_column(Numeric(6, 2))
    pieces: Mapped[dict] = mapped_column(JSON, default=dict)  # références des pièces justificatives
    validation: Mapped[ValidationLivreur] = mapped_column(
        enum_texte(ValidationLivreur), default=ValidationLivreur.EN_ATTENTE
    )
    note: Mapped[float | None] = mapped_column(Numeric(3, 2))
    en_ligne: Mapped[bool] = mapped_column(default=False)

    utilisateur: Mapped[Utilisateur] = relationship(back_populates="livreur")


class Commande(Base):
    __tablename__ = "commandes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("utilisateurs.id"), index=True)
    livreur_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("livreurs.utilisateur_id"), index=True)
    adresse_enlevement: Mapped[str] = mapped_column(String(255))
    position_enlevement = mapped_column(point())
    adresse_livraison: Mapped[str] = mapped_column(String(255))
    position_livraison = mapped_column(point())
    description_colis: Mapped[str] = mapped_column(String(255), default="")
    poids_kg: Mapped[float] = mapped_column(Numeric(6, 2))
    priorite: Mapped[int] = mapped_column(Integer, default=0)
    prix_fcfa: Mapped[int | None] = mapped_column(Integer)
    etat: Mapped[EtatCommande] = mapped_column(
        enum_texte(EtatCommande), default=EtatCommande.CREEE, index=True
    )
    cree_le: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    attribuee_le: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enlevee_le: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    livree_le: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    livreur_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("livreurs.utilisateur_id"))
    position = mapped_column(point())
    precision_m: Mapped[float | None] = mapped_column(Numeric(7, 2))
    enregistree_le: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_positions_livreur_date", "livreur_id", "enregistree_le"),)


class Offre(Base):
    __tablename__ = "offres"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    commande_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("commandes.id"), index=True)
    livreur_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("livreurs.utilisateur_id"))
    cout: Mapped[float] = mapped_column(Numeric(10, 2))
    arrivee_estimee: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resultat: Mapped[ResultatOffre] = mapped_column(
        enum_texte(ResultatOffre), default=ResultatOffre.PROPOSEE
    )
    cree_le: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Paiement(Base):
    __tablename__ = "paiements"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    commande_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("commandes.id"), index=True)
    mode: Mapped[ModePaiement] = mapped_column(enum_texte(ModePaiement))
    montant_fcfa: Mapped[int] = mapped_column(Integer)
    reference_prestataire: Mapped[str | None] = mapped_column(String(120))
    etat: Mapped[EtatPaiement] = mapped_column(enum_texte(EtatPaiement), default=EtatPaiement.EN_ATTENTE)
    cree_le: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Facture(Base):
    __tablename__ = "factures"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    numero: Mapped[str] = mapped_column(String(30), unique=True)
    commande_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("commandes.id"), index=True)
    montant_fcfa: Mapped[int] = mapped_column(Integer)
    chemin_pdf: Mapped[str | None] = mapped_column(String(255))
    cree_le: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Evenement(Base):
    """Journal de bord : une ligne par changement d'état ou message important."""

    __tablename__ = "evenements"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    commande_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("commandes.id"), index=True)
    type: Mapped[str] = mapped_column(String(50))
    auteur_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("utilisateurs.id"))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    cree_le: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CodeVerification(Base):
    """Code à usage unique envoyé par SMS (stocké haché, jamais en clair)."""

    __tablename__ = "codes_verification"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    telephone: Mapped[str] = mapped_column(String(20), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    expire_le: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    tentatives: Mapped[int] = mapped_column(Integer, default=0)
    utilise: Mapped[bool] = mapped_column(default=False)
    cree_le: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
