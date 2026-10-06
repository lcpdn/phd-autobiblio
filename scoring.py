"""Calcul du score de priorité — familles A et E.

    score = α·p(impact) + β·p(fraîcheur) + γ·p(thème) + bonus_survey + urgence − ρ·redondance

où p(·) est le rang-percentile du terme sur le vivier du jour. La normalisation par rang
est ce qui permet aux pondérations d'exprimer des priorités relatives plutôt que des
différences d'échelle entre des grandeurs qui ne varient pas dans les mêmes bornes.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import statistics
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .openalex import Citation
from .zotlib import Reference

MOTS_VIDES = {
    "the", "and", "for", "with", "from", "that", "this", "are", "was", "were", "its", "their",
    "into", "über", "les", "des", "une", "aux", "dans", "pour", "par", "sur", "avec", "est",
    "sont", "cette", "chez", "entre", "vers", "plus", "leur", "leurs", "nous", "vous",
    "using", "based", "toward", "towards", "approach", "study", "analysis", "case",
}
_RE_MOT = re.compile(r"[a-z0-9]{3,}")


@dataclass
class Candidat:
    reference: Reference
    age: float | None = None
    citations: int | None = None
    citations_imputees: bool = False
    demi_vie: float = 3.0
    impact: float = 0.0
    fraicheur: float = 0.0
    theme: float = 0.0
    p_impact: float = 0.0
    p_fraicheur: float = 0.0
    p_theme: float = 0.0
    bonus_survey: float = 0.0
    urgence: float = 0.0
    redondance: float = 0.0
    score: float = 0.0
    motifs: list[str] = field(default_factory=list)

    def variables(self) -> dict:
        """Bloc de variables destiné à l'instrumentation des fiches (§0.3 du document)."""
        return {
            "impact": round(self.p_impact, 4),
            "fraicheur": round(self.p_fraicheur, 4),
            "theme": round(self.p_theme, 4),
            "bonus_survey": round(self.bonus_survey, 4),
            "urgence": round(self.urgence, 4),
            "redondance": round(self.redondance, 4),
            "citations": self.citations,
            "citations_imputees": self.citations_imputees,
            "demi_vie": self.demi_vie,
            "age": round(self.age, 2) if self.age is not None else None,
        }


# --------------------------------------------------------------------- outils


def rangs_percentiles(valeurs: Sequence[float]) -> list[float]:
    """Rang-percentile dans [0, 1], ex æquo traités par rang moyen."""
    n = len(valeurs)
    if n == 0:
        return []
    if n == 1:
        return [0.5]
    ordre = sorted(range(n), key=lambda i: valeurs[i])
    resultat = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and valeurs[ordre[j + 1]] == valeurs[ordre[i]]:
            j += 1
        moyen = (i + j) / 2
        for k in range(i, j + 1):
            resultat[ordre[k]] = moyen / (n - 1)
        i = j + 1
    return resultat


def _normaliser(texte: str) -> list[str]:
    sans_accents = unicodedata.normalize("NFD", texte.lower())
    sans_accents = "".join(c for c in sans_accents if unicodedata.category(c) != "Mn")
    return [m for m in _RE_MOT.findall(sans_accents) if m not in MOTS_VIDES]


def _vecteurs_tfidf(documents: list[str]) -> list[dict[str, float]]:
    """TF-IDF creux, en Python pur : suffisant pour des titres et des résumés courts."""
    sacs = [dict() for _ in documents]
    for index, document in enumerate(documents):
        for mot in _normaliser(document):
            sacs[index][mot] = sacs[index].get(mot, 0.0) + 1.0
    n = max(len(documents), 1)
    frequences: dict[str, int] = {}
    for sac in sacs:
        for mot in sac:
            frequences[mot] = frequences.get(mot, 0) + 1
    vecteurs = []
    for sac in sacs:
        vecteur = {}
        for mot, tf in sac.items():
            idf = math.log((1 + n) / (1 + frequences[mot])) + 1.0
            vecteur[mot] = (1 + math.log(tf)) * idf
        norme = math.sqrt(sum(v * v for v in vecteur.values())) or 1.0
        vecteurs.append({m: v / norme for m, v in vecteur.items()})
    return vecteurs


def _cosinus(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(valeur * b.get(mot, 0.0) for mot, valeur in a.items())


def _texte(reference: Reference) -> str:
    return f"{reference.titre} {reference.publication} {reference.resume}"


# ------------------------------------------------------------------- termes A


def demi_vie(reference: Reference, cfg: dict) -> float:
    """Demi-vie de fraîcheur, différenciée par littérature (§A.2)."""
    par_tag: dict = cfg["scoring"]["demi_vie_par_tag"] or {}
    valeurs = [par_tag[t.lower()] for t in reference.tags if t.lower() in par_tag]
    return min(valeurs) if valeurs else cfg["scoring"]["demi_vie_defaut"]


def terme_impact(citations: int | None, age: float | None, cfg: dict) -> float | None:
    """log(1 + c / (a + a₀)) : impact normalisé par l'âge, amorti pour les articles récents."""
    if citations is None or age is None:
        return None
    return math.log(1 + citations / (max(age, 0.0) + cfg["scoring"]["amortissement_age"]))


def terme_fraicheur(age: float | None, h: float) -> float:
    if age is None:
        return 0.0
    return math.exp(-max(age, 0.0) * math.log(2) / max(h, 0.1))


def terme_theme(reference: Reference, cfg: dict) -> tuple[float, list[str]]:
    """Priorité thématique graduée, avec bonus de pont entre questions de recherche (§A.4)."""
    poids: dict = cfg["scoring"]["poids_tags"] or {}
    questions = {q.lower() for q in cfg["scoring"]["tags_questions"]}
    total = 0.0
    touchees = set()
    motifs = []
    for tag in reference.tags:
        cle = tag.lower()
        if cle in poids:
            total += poids[cle]
            motifs.append(cle)
        if cle in questions:
            touchees.add(cle)
    valeur = min(1.0, total)
    if len(touchees) >= 2:
        valeur += cfg["scoring"]["bonus_pont"]
        motifs.append("pont")
    return valeur, motifs


def est_survey(reference: Reference, cfg: dict) -> bool:
    marqueurs = [m.lower() for m in cfg["scoring"]["types_survey"]]
    titre = reference.titre.lower()
    return any(m in titre for m in marqueurs) or reference.type_zotero == "review"


def urgence(reference: Reference, cfg: dict, aujourdhui: dt.date) -> float:
    """Bonus croissant à l'approche d'une échéance de rédaction (§E.2)."""
    parametres = cfg.get("echeance") or {}
    date_limite = parametres.get("date")
    tags = parametres.get("tags") or []
    if not date_limite or not tags or not reference.a_tag(tags):
        return 0.0
    if isinstance(date_limite, str):
        date_limite = dt.date.fromisoformat(date_limite)
    elif isinstance(date_limite, dt.datetime):
        date_limite = date_limite.date()
    restants = (date_limite - aujourdhui).days
    if restants < 0:
        return 0.0
    return min(parametres.get("kappa", 20.0) / max(restants, 1), parametres.get("plafond", 1.0))


# --------------------------------------------------------------------- calcul


def calculer(
    candidats_refs: Iterable[Reference],
    donnees_citations: dict[str, Citation],
    references_lues: Sequence[Reference],
    phase: dict,
    cfg: dict,
    aujourdhui: dt.date | None = None,
) -> list[Candidat]:
    """Produit la liste des candidats scorés, triée par score décroissant."""
    aujourdhui = aujourdhui or dt.date.today()
    annee_courante = aujourdhui.year + (aujourdhui.timetuple().tm_yday / 365.25)
    candidats = [Candidat(reference=r) for r in candidats_refs]

    # 1. Termes bruts
    for candidat in candidats:
        reference = candidat.reference
        citation = donnees_citations.get(reference.doi) if reference.doi else None
        candidat.citations = citation.citations if citation and citation.trouve else None
        annee = reference.annee or (citation.annee if citation else None)
        candidat.age = max(annee_courante - annee, 0.0) if annee else None
        candidat.demi_vie = demi_vie(reference, cfg)
        candidat.fraicheur = terme_fraicheur(candidat.age, candidat.demi_vie)
        candidat.theme, candidat.motifs = terme_theme(reference, cfg)
        candidat.bonus_survey = phase.get("bonus_survey", 0.0) if est_survey(reference, cfg) else 0.0
        candidat.urgence = urgence(reference, cfg, aujourdhui)

    # 2. Impact, avec imputation explicite des données OpenAlex manquantes (§A.3)
    bruts = {
        id(c): terme_impact(c.citations, c.age, cfg) for c in candidats
    }
    connus = [v for v in bruts.values() if v is not None]
    mediane_globale = statistics.median(connus) if connus else 0.0
    par_annee: dict[int, list[float]] = {}
    for candidat in candidats:
        valeur = bruts[id(candidat)]
        if valeur is not None and candidat.reference.annee:
            par_annee.setdefault(candidat.reference.annee, []).append(valeur)

    for candidat in candidats:
        valeur = bruts[id(candidat)]
        if valeur is not None:
            candidat.impact = valeur
            continue
        candidat.citations_imputees = True
        if not cfg["scoring"]["imputation_openalex"]:
            candidat.impact = 0.0
            continue
        meme_annee = par_annee.get(candidat.reference.annee or -1)
        candidat.impact = statistics.median(meme_annee) if meme_annee else mediane_globale

    # 3. Redondance : similarité maximale à ce qui a déjà été lu (§A.5)
    rho = cfg["scoring"]["redondance"]
    if rho and references_lues and candidats:
        textes = [_texte(c.reference) for c in candidats] + [_texte(r) for r in references_lues]
        vecteurs = _vecteurs_tfidf(textes)
        vecteurs_lus = vecteurs[len(candidats):]
        for index, candidat in enumerate(candidats):
            candidat.redondance = max(
                (_cosinus(vecteurs[index], v) for v in vecteurs_lus), default=0.0
            )

    # 4. Normalisation par rang puis combinaison linéaire
    p_impact = rangs_percentiles([c.impact for c in candidats])
    p_fraicheur = rangs_percentiles([c.fraicheur for c in candidats])
    p_theme = rangs_percentiles([c.theme for c in candidats])
    alpha, beta, gamma = phase.get("alpha", 1.0), phase.get("beta", 1.5), phase.get("gamma", 2.0)

    for index, candidat in enumerate(candidats):
        candidat.p_impact = p_impact[index]
        candidat.p_fraicheur = p_fraicheur[index]
        candidat.p_theme = p_theme[index]
        candidat.score = (
            alpha * candidat.p_impact
            + beta * candidat.p_fraicheur
            + gamma * candidat.p_theme
            + candidat.bonus_survey
            + candidat.urgence
            - rho * candidat.redondance
        )

    candidats.sort(key=lambda c: c.score, reverse=True)
    return candidats


# ------------------------------------------------------------------ sélection


def selectionner(
    candidats: list[Candidat],
    cfg: dict,
    efforts: dict[str, float] | None = None,
) -> list[Candidat]:
    """Choisit les lectures du jour : par cardinal, ou sous budget d'effort (§B.3).

    `efforts` associe une clé Zotero à un coût de lecture en unités ; les clés absentes
    reçoivent l'effort par défaut de la configuration.
    """
    parametres = cfg["selection"]
    seuil = parametres.get("seuil_plancher")
    vivier = [c for c in candidats if seuil is None or c.score >= seuil]
    if not vivier:
        return []

    efforts = efforts or {}
    defaut = parametres.get("effort_defaut", 1.0)
    diversite = parametres.get("diversite", 0.0)

    retenus: list[Candidat] = []
    vecteurs: dict[int, dict[str, float]] = {}
    if diversite:
        textes = [_texte(c.reference) for c in vivier]
        for index, vecteur in enumerate(_vecteurs_tfidf(textes)):
            vecteurs[id(vivier[index])] = vecteur

    def valeur(candidat: Candidat) -> float:
        """Apport marginal : score pur, ou MMR si la diversité est activée."""
        if not diversite or not retenus:
            return candidat.score
        proximite = max(
            _cosinus(vecteurs[id(candidat)], vecteurs[id(r)]) for r in retenus
        )
        return (1 - diversite) * candidat.score - diversite * proximite

    if parametres.get("mode") == "budget":
        budget = float(parametres.get("budget_effort", 2.0))
        restants = list(vivier)
        while restants:
            restants.sort(key=valeur, reverse=True)
            choisi = None
            for candidat in restants:
                cout = efforts.get(candidat.reference.cle, defaut)
                if cout <= budget or not retenus:  # au moins une lecture, même longue
                    choisi = candidat
                    break
            if choisi is None:
                break
            budget -= efforts.get(choisi.reference.cle, defaut)
            retenus.append(choisi)
            restants.remove(choisi)
            if budget <= 0:
                break
        return retenus

    n = int(parametres.get("n", 2))
    restants = list(vivier)
    while restants and len(retenus) < n:
        restants.sort(key=valeur, reverse=True)
        retenus.append(restants.pop(0))
    return retenus
