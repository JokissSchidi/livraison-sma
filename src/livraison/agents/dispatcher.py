"""Agent Dispatcher : initiateur du Contract Net (cahier des charges § 3.3).

Pour chaque commande : consulte l'Annuaire, lance l'appel d'offres (CFP) aux livreurs proches,
collecte les offres pendant un délai T, retient la meilleure, la fait confirmer par le livreur,
et passe à l'offre suivante en cas de refus ou de silence. Une commande sans offre entre en file
d'attente et est relancée avec un rayon élargi (F-33) ; au bout de N tentatives, le répartiteur
est alerté. Plusieurs commandes sont traitées en parallèle, chacune dans sa propre conversation
(identifiant de commande = `thread` des messages, F-32).
"""
from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from spade.agent import Agent
from spade.behaviour import CyclicBehaviour

from livraison.agents import messages as m
from livraison.modele.offre import Coefficients, Demande, Proposition, Refus, classer
from livraison.services.itineraires import Coord

journal = logging.getLogger("livraison.dispatcher")


class Statut(enum.StrEnum):
    EN_COURS = "en_cours"
    ATTRIBUEE = "attribuee"
    EN_FILE = "en_file"
    ECHEC = "echec"
    ANNULEE = "annulee"


@dataclass(frozen=True)
class ReglagesDispatcher:
    """Paramètres du Dispatcher (réglables par l'administrateur à l'étape suivante)."""

    rayon_initial_m: float = 3_000.0
    facteur_rayon: float = 2.0  # le rayon est multiplié par ce facteur à chaque relance
    max_tentatives: int = 4  # au-delà : alerte au répartiteur
    max_candidats: int = 20  # livreurs présélectionnés (les plus proches)
    delai_offres_s: float = 3.0  # T : durée de collecte des offres
    delai_acceptation_s: float = 5.0  # réponse technique de l'agent Livreur
    delai_confirmation_s: float = 30.0  # confirmation humaine du livreur
    delai_relance_s: float = 30.0  # pause entre deux relances de la file d'attente
    delai_annuaire_s: float = 3.0
    age_gps_max_s: float = 120.0
    coef: Coefficients = field(default_factory=Coefficients)


# Confirmation humaine : (livreur_id, commande_id, demande, proposition, delai_s) -> accepte ?
Confirmation = Callable[[str, str, Demande, Proposition, float], Awaitable[bool]]


async def confirmation_automatique(*_args) -> bool:
    """Développement et simulations : le livreur accepte toujours. Remplacée par l'application mobile."""
    return True


@dataclass(frozen=True)
class Attribution:
    commande_id: str
    livreur_id: str
    jid: str
    proposition: Proposition
    tentative: int


@dataclass(frozen=True)
class ResultatAttribution:
    statut: Statut
    attribution: Attribution | None = None
    motifs: tuple[str, ...] = ()


@dataclass
class Suivi:
    commande_id: str
    demande: Demande
    urgente: bool
    cree_le: datetime = field(default_factory=lambda: datetime.now(UTC))
    statut: Statut = Statut.EN_COURS
    tentatives: int = 0
    rayon_m: float = 0.0
    exclus: set[str] = field(default_factory=set)  # livreurs qui ont décliné ou n'ont pas répondu
    attribution: Attribution | None = None
    motifs: list[str] = field(default_factory=list)
    verrou: asyncio.Lock = field(default_factory=asyncio.Lock)
    fin: asyncio.Event = field(default_factory=asyncio.Event)

    def resultat(self) -> ResultatAttribution:
        return ResultatAttribution(self.statut, self.attribution, tuple(self.motifs))


class DispatcherAgent(Agent):
    def __init__(
        self,
        jid,
        mot_de_passe,
        annuaire_jid: str,
        reglages: ReglagesDispatcher | None = None,
        confirmer: Confirmation = confirmation_automatique,
        sur_evenement: Callable[[str, str, dict], None] | None = None,
        signaler: Callable[[str, str], None] | None = None,
        **kwargs,
    ):
        super().__init__(jid, mot_de_passe, **kwargs)
        self.annuaire_jid = annuaire_jid
        self.reglages = reglages or ReglagesDispatcher()
        self.confirmer = confirmer
        self._sur_evenement = sur_evenement  # (commande_id, type, détail) : journal des événements
        self._signaler = signaler  # (commande_id, raison) : alerte au répartiteur
        self.suivis: dict[str, Suivi] = {}
        self._conversations: dict[str, asyncio.Queue] = {}
        self._sortie: asyncio.Queue = asyncio.Queue()
        self._reveil = asyncio.Event()
        self._tache_file: asyncio.Task | None = None

    # ------------------------------------------------------------------ cycle de vie
    async def setup(self):
        self.add_behaviour(self.Recevoir())
        self.add_behaviour(self.Expedier())
        self._tache_file = asyncio.create_task(self._boucle_file())

    async def stop(self):
        if self._tache_file:
            self._tache_file.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._tache_file
        await super().stop()

    class Recevoir(CyclicBehaviour):
        """Range chaque message reçu dans la conversation de sa commande."""

        async def run(self):
            msg = await self.receive(timeout=1)
            if msg is None:
                return
            file = self.agent._conversations.get(msg.thread)
            if file is not None:
                file.put_nowait(msg)
            else:
                journal.debug("Message hors conversation ignoré (thread=%s)", msg.thread)

    class Expedier(CyclicBehaviour):
        """Envoie les messages mis en file par les attributions en cours."""

        async def run(self):
            try:
                msg = await asyncio.wait_for(self.agent._sortie.get(), timeout=1)
            except TimeoutError:
                return
            await self.send(msg)

    def _envoyer(self, msg) -> None:
        self._sortie.put_nowait(msg)

    @contextlib.asynccontextmanager
    async def _conversation(self, thread: str):
        file: asyncio.Queue = asyncio.Queue()
        self._conversations[thread] = file
        try:
            yield file
        finally:
            self._conversations.pop(thread, None)

    @staticmethod
    async def _attendre(file: asyncio.Queue, jusqu_a: float, accepte=lambda _msg: True):
        """Premier message qui satisfait `accepte`, ou None à l'échéance (les autres sont écartés)."""
        while True:
            reste = jusqu_a - time.monotonic()
            if reste <= 0:
                return None
            try:
                msg = await asyncio.wait_for(file.get(), timeout=reste)
            except TimeoutError:
                return None
            if accepte(msg):
                return msg

    def _evenement(self, commande_id: str, type_: str, **detail) -> None:
        journal.info("[%s] %s %s", commande_id, type_, detail or "")
        if self._sur_evenement:
            try:
                self._sur_evenement(commande_id, type_, detail)
            except Exception:  # noqa: BLE001  (un journal défaillant ne doit pas bloquer une livraison)
                journal.exception("Échec de l'enregistrement de l'événement %s", type_)

    # ------------------------------------------------------------------ interface publique
    async def soumettre(self, commande_id: str, demande: Demande, urgente: bool = False) -> ResultatAttribution:
        """Lance l'attribution d'une commande. Sans offre, elle entre en file d'attente (relances automatiques)."""
        if commande_id in self.suivis:
            return self.suivis[commande_id].resultat()
        suivi = Suivi(commande_id, demande, urgente, rayon_m=self.reglages.rayon_initial_m)
        self.suivis[commande_id] = suivi
        self._evenement(commande_id, "soumise", urgente=urgente)
        async with suivi.verrou:
            await self._essai(suivi)
        return suivi.resultat()

    async def attendre(self, commande_id: str, delai_s: float = 30.0) -> ResultatAttribution:
        """Attend que la commande soit attribuée, en échec ou annulée (utile pour la file d'attente)."""
        suivi = self.suivis[commande_id]
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(suivi.fin.wait(), timeout=delai_s)
        return suivi.resultat()

    def reveiller(self) -> None:
        """À appeler quand un livreur passe en ligne : la file d'attente est retentée tout de suite."""
        self._reveil.set()

    def en_file(self) -> list[str]:
        return [s.commande_id for s in self._ordre_file()]

    async def reattribuer(self, commande_id: str, raison: str) -> ResultatAttribution:
        """Retire la course au livreur actuel (panne, absence de signal, décision du répartiteur) et relance l'appel d'offres."""
        suivi = self.suivis[commande_id]
        async with suivi.verrou:
            if suivi.statut in (Statut.ANNULEE, Statut.ECHEC):
                return suivi.resultat()
            ancien = suivi.attribution
            if ancien is not None:
                suivi.exclus.add(ancien.livreur_id)
                self._envoyer(m.creer(ancien.jid, m.CANCEL, {}, thread=commande_id))
            self._evenement(commande_id, "reattribution", raison=raison,
                            ancien=ancien.livreur_id if ancien else None)
            suivi.attribution, suivi.statut = None, Statut.EN_COURS
            suivi.fin.clear()
            suivi.rayon_m, suivi.tentatives = self.reglages.rayon_initial_m, 0
            await self._essai(suivi)
        return suivi.resultat()

    async def annuler(self, commande_id: str) -> None:
        suivi = self.suivis.get(commande_id)
        if suivi is None:
            return
        async with suivi.verrou:
            if suivi.attribution is not None:
                self._envoyer(m.creer(suivi.attribution.jid, m.CANCEL, {}, thread=commande_id))
            suivi.statut, suivi.attribution = Statut.ANNULEE, None
            suivi.fin.set()
            self._evenement(commande_id, "annulee")

    # ------------------------------------------------------------------ file d'attente (F-33, F-35)
    def _ordre_file(self) -> list[Suivi]:
        en_file = [s for s in self.suivis.values() if s.statut == Statut.EN_FILE]
        return sorted(en_file, key=lambda s: (not s.urgente, s.cree_le))  # urgentes d'abord, puis les plus anciennes

    async def _boucle_file(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._reveil.wait(), timeout=self.reglages.delai_relance_s)
                compter = False  # réveil par un nouveau livreur : pas une tentative de plus
            except TimeoutError:
                compter = True
            self._reveil.clear()
            for suivi in self._ordre_file():
                async with suivi.verrou:
                    if suivi.statut != Statut.EN_FILE:
                        continue
                    if compter:
                        suivi.rayon_m *= self.reglages.facteur_rayon
                    await self._essai(suivi, compter_tentative=compter)

    # ------------------------------------------------------------------ une tentative d'attribution
    async def _essai(self, suivi: Suivi, compter_tentative: bool = True) -> None:
        r = self.reglages
        if compter_tentative:
            suivi.tentatives += 1
        suivi.statut = Statut.EN_COURS
        try:
            attribution = await self._contract_net(suivi)
        except Exception:  # noqa: BLE001
            journal.exception("[%s] erreur pendant l'attribution", suivi.commande_id)
            attribution = None
            suivi.motifs.append("erreur interne")
        if attribution is not None:
            suivi.attribution, suivi.statut = attribution, Statut.ATTRIBUEE
            suivi.fin.set()
            self._evenement(suivi.commande_id, "attribuee", livreur=attribution.livreur_id,
                            cout=attribution.proposition.cout, tentative=suivi.tentatives)
        elif suivi.tentatives >= r.max_tentatives:
            suivi.statut = Statut.ECHEC
            suivi.fin.set()
            self._evenement(suivi.commande_id, "echec", tentatives=suivi.tentatives)
            if self._signaler:
                self._signaler(suivi.commande_id, f"aucune offre après {suivi.tentatives} tentatives")
        else:
            suivi.statut = Statut.EN_FILE
            self._evenement(suivi.commande_id, "en_file", tentatives=suivi.tentatives, rayon_m=suivi.rayon_m)

    async def _contract_net(self, suivi: Suivi) -> Attribution | None:
        r = self.reglages
        cmd = suivi.commande_id
        candidats = await self._chercher(suivi)
        if not candidats:
            suivi.motifs.append("aucun livreur en ligne dans le rayon")
            return None

        # Étapes 1 et 2 : appel d'offres, collecte pendant T
        propositions: dict[str, Proposition] = {}
        refus: dict[str, Refus] = {}
        async with self._conversation(cmd) as file:
            for c in candidats:
                self._envoyer(m.creer(c["jid"], m.CFP, {"demande": m.demande_vers_json(suivi.demande)}, thread=cmd))
            self._evenement(cmd, "cfp_envoye", livreurs=[c["livreur_id"] for c in candidats], rayon_m=suivi.rayon_m)
            attendus = {c["livreur_id"] for c in candidats}
            echeance = time.monotonic() + r.delai_offres_s
            while len(propositions) + len(refus) < len(attendus):
                msg = await self._attendre(file, echeance, lambda x: m.performative(x) in (m.PROPOSE, m.REFUSE))
                if msg is None:
                    break
                corps = m.lire(msg)
                if corps.get("livreur_id") not in attendus:
                    continue
                if m.performative(msg) == m.PROPOSE:
                    propositions[corps["livreur_id"]] = m.proposition_depuis_json(corps)
                else:
                    refus[corps["livreur_id"]] = m.refus_depuis_json(corps)
        for lid, rf in refus.items():
            self._evenement(cmd, "offre_refusee", livreur=lid, motif=rf.motif.value)
        for lid, p in propositions.items():
            self._evenement(cmd, "offre_recue", livreur=lid, cout=p.cout, approche_s=p.approche_s,
                            detour_s=p.detour_s, penalite_s=p.penalite_s, arrivee=p.arrivee_estimee.isoformat())
        if not propositions:
            suivi.motifs.append("tous les livreurs ont refusé ou n'ont pas répondu")
            return None

        # Étapes 3 et 4 : meilleure offre d'abord, puis la suivante si elle échoue
        jids = {c["livreur_id"]: c["jid"] for c in candidats}
        classement = classer(list(propositions.values()), r.coef)
        gagnante: Proposition | None = None
        for prop in classement:
            if await self._proposer(suivi, prop, jids[prop.livreur_id]):
                gagnante = prop
                break
        for prop in classement:  # les autres sont prévenus que leur offre n'est pas retenue
            if prop is not gagnante and prop.livreur_id not in suivi.exclus:
                self._envoyer(m.creer(jids[prop.livreur_id], m.REJECT, {}, thread=cmd))
        if gagnante is None:
            suivi.motifs.append("aucune offre confirmée")
            return None
        return Attribution(cmd, gagnante.livreur_id, jids[gagnante.livreur_id], gagnante, suivi.tentatives)

    async def _proposer(self, suivi: Suivi, prop: Proposition, jid: str) -> bool:
        """ACCEPT_PROPOSAL, puis confirmation humaine. Renvoie True si la course est ferme."""
        r, cmd = self.reglages, suivi.commande_id
        async with self._conversation(cmd) as file:
            self._envoyer(m.creer(jid, m.ACCEPT, {"demande": m.demande_vers_json(suivi.demande)}, thread=cmd))
            self._evenement(cmd, "accept_envoye", livreur=prop.livreur_id, cout=prop.cout)
            reponse = await self._attendre(
                file, time.monotonic() + r.delai_acceptation_s,
                lambda x: m.performative(x) in (m.INFORM, m.FAILURE) and str(x.sender.bare) == jid)
        if reponse is None or m.performative(reponse) == m.FAILURE:
            motif = "silence" if reponse is None else "échec technique"
            self._evenement(cmd, "acceptation_impossible", livreur=prop.livreur_id, motif=motif)
            if reponse is None:  # silence : on le met de côté pour cette commande
                suivi.exclus.add(prop.livreur_id)
                self._envoyer(m.creer(jid, m.CANCEL, {}, thread=cmd))
            return False  # un échec technique (livreur devenu saturé) est passager : pas d'exclusion
        try:
            accepte = await asyncio.wait_for(
                self.confirmer(prop.livreur_id, cmd, suivi.demande, prop, r.delai_confirmation_s),
                timeout=r.delai_confirmation_s)
        except TimeoutError:
            accepte = False
            self._evenement(cmd, "confirmation_absente", livreur=prop.livreur_id)
        else:
            if not accepte:
                self._evenement(cmd, "confirmation_refusee", livreur=prop.livreur_id)
        if not accepte:
            suivi.exclus.add(prop.livreur_id)
            self._envoyer(m.creer(jid, m.CANCEL, {}, thread=cmd))  # libère la place du livreur
        return accepte

    async def _chercher(self, suivi: Suivi) -> list[dict]:
        """Présélection : livreurs en ligne les plus proches du point d'enlèvement (hors exclus)."""
        r, cmd = self.reglages, suivi.commande_id
        thread = f"{cmd}#annuaire"
        async with self._conversation(thread) as file:
            self._envoyer(m.creer(self.annuaire_jid, m.REQUEST, {
                "action": "chercher", "centre": m.coord_vers_json(suivi.demande.enlevement),
                "rayon_m": suivi.rayon_m, "age_max_s": r.age_gps_max_s}, thread=thread))
            reponse = await self._attendre(file, time.monotonic() + r.delai_annuaire_s,
                                           lambda x: m.performative(x) == m.INFORM)
        if reponse is None:
            self._evenement(cmd, "annuaire_muet")
            return []
        livreurs = [x for x in m.lire(reponse)["livreurs"] if x["livreur_id"] not in suivi.exclus]
        return livreurs[: r.max_candidats]
