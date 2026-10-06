"""Interface en ligne de commande du sélecteur de lectures.

Commandes :
    verifier      diagnostic de l'installation (Zotero, chemins, clé, dépendances)
    autoriser     demande à Zotero une clé d'API locale persistante
    exporter      exporte le corpus normalisé (CSV + JSON)
    qc            contrôle qualité ; écrit dans Zotero avec --ecrire
    pdf CLE       affiche le chemin du PDF d'une référence
    selectionner  chaîne nocturne : score, classement, choix du jour, transfert
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import VERSION_ALGO, ErreurConfig, charger, phase_active, repertoire
from .fiches import index_traites, inventaire, pertinences_par_doi
from .openalex import Cache, citations, nombre_pages
from .qc import analyser, appliquer, planifier
from .scoring import calculer, selectionner
from .zotlib import AutorisationRequise, ClientZotero, ZoteroIndisponible

journal = logging.getLogger("selecteur")

CODE_OK, CODE_ERREUR, CODE_ZOTERO = 0, 1, 2

COLONNES = [
    "rang", "score", "cle_zotero", "titre", "annee", "doi", "type", "tags",
    "citations", "citations_imputees", "age", "demi_vie",
    "p_impact", "p_fraicheur", "p_theme", "bonus_survey", "urgence", "redondance",
    "effort", "pdf", "cle_bbt",
]


def _configurer_journal(cfg: dict, bavard: bool) -> None:
    niveau = logging.DEBUG if bavard else logging.INFO
    logging.basicConfig(
        level=niveau,
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stderr,
    )
    try:
        fichier = repertoire(cfg, "sorties") / "selecteur.log"
        handler = logging.FileHandler(fichier, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s  %(levelname)-7s %(name)s  %(message)s", "%Y-%m-%d %H:%M:%S"
        ))
        logging.getLogger().addHandler(handler)
    except OSError as err:
        journal.warning("Journal fichier indisponible : %s", err)


def _client(cfg: dict) -> ClientZotero:
    client = ClientZotero(cfg)
    client.verifier()
    return client


def _consigner(cfg: dict, evenement: dict) -> None:
    """Ajoute une ligne au journal d'exécution (traçabilité pour le journal de bord)."""
    fichier = repertoire(cfg, "sorties") / "journal.jsonl"
    with fichier.open("a", encoding="utf-8") as flux:
        flux.write(json.dumps(evenement, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------ commandes


def cmd_verifier(args, cfg: dict) -> int:
    print(f"Sélecteur de lectures {__version__} — algorithme {VERSION_ALGO}")
    print(f"Configuration : {cfg['_fichier']}" + ("" if cfg["_existe"] else "  [ABSENTE — défauts]"))

    nom, phase = phase_active(cfg)
    print(f"Phase active  : {nom}  (α={phase.get('alpha')}, β={phase.get('beta')}, "
          f"γ={phase.get('gamma')})")

    for cle, chemin in cfg["chemins"].items():
        existe = Path(chemin).exists()
        print(f"  {cle:<12} {chemin}  {'✓' if existe else '✗ absent'}")

    try:
        import pypdf  # noqa: F401
        print("  pypdf        installé (estimation de l'effort de lecture disponible)")
    except ImportError:
        print("  pypdf        absent — effort de lecture estimé à la valeur par défaut")

    client = ClientZotero(cfg)
    try:
        etat = client.verifier()
    except ZoteroIndisponible as err:
        print(f"\nZotero : INJOIGNABLE\n  {err}")
        return CODE_ZOTERO
    print(f"\nZotero : joignable — {etat['items']} éléments, server_id {etat['server_id']}")
    print("Clé d'API locale : " + ("présente" if client.a_cle else "absente (lecture seule)"))

    fiches = inventaire(cfg)
    print(f"Fiches de lecture : {len(fiches)} "
          f"({sum(1 for f in fiches if f.doi)} appariables par DOI)")
    return CODE_OK


def cmd_autoriser(args, cfg: dict) -> int:
    client = ClientZotero(cfg)
    client.verifier()
    print("Zotero va afficher une boîte de dialogue.")
    print("Choisir « Toujours autoriser » : une clé à usage unique serait consommée par")
    print("la première écriture et rendrait la tâche cron inopérante dès le lendemain.")
    reponse = client.autoriser()
    if reponse.get("remember"):
        print(f"Clé persistante enregistrée dans {client.chemin_cle} (permissions 600).")
        return CODE_OK
    print("Clé à usage unique accordée — non enregistrée. Relancer et choisir "
          "« Toujours autoriser » pour un fonctionnement automatisé.")
    return CODE_ERREUR


def cmd_pdf(args, cfg: dict) -> int:
    client = _client(cfg)
    chemin = client.chemin_pdf(args.cle)
    if not chemin:
        print(f"Aucun PDF joint à {args.cle}.", file=sys.stderr)
        return CODE_ERREUR
    print(chemin)
    return CODE_OK


def cmd_exporter(args, cfg: dict) -> int:
    client = _client(cfg)
    references = client.references()
    fiches = inventaire(cfg)
    dois_traites, cles_traitees = index_traites(fiches)
    sorties = repertoire(cfg, "sorties")
    horodatage = dt.date.today().isoformat()

    lignes = []
    for reference in references:
        lignes.append({
            "cle_zotero": reference.cle,
            "titre": reference.titre,
            "annee": reference.annee,
            "doi": reference.doi,
            "type": reference.type_zotero,
            "tags": ";".join(reference.tags),
            "cle_bbt": reference.cle_bbt,
            "publication": reference.publication,
            "traite": int(
                (reference.doi in dois_traites) or (reference.cle in cles_traitees)
            ),
        })

    chemin_csv = sorties / f"corpus_{horodatage}.csv"
    with chemin_csv.open("w", encoding="utf-8", newline="") as flux:
        writer = csv.DictWriter(flux, fieldnames=list(lignes[0]) if lignes else ["cle_zotero"])
        writer.writeheader()
        writer.writerows(lignes)

    chemin_json = sorties / f"corpus_{horodatage}.json"
    chemin_json.write_text(
        json.dumps(lignes, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"{len(lignes)} références exportées.\n  {chemin_csv}\n  {chemin_json}")
    return CODE_OK


def cmd_qc(args, cfg: dict) -> int:
    client = _client(cfg)
    references = client.references()
    anomalies = analyser(references, cfg, client if cfg["qc"]["signaler_sans_pdf"] else None)
    plans = planifier(references, anomalies, cfg)

    par_type: dict[str, int] = {}
    for anomalie in anomalies:
        par_type[anomalie.type] = par_type.get(anomalie.type, 0) + 1
    print(f"{len(references)} références analysées.")
    for type_anomalie, nombre in sorted(par_type.items()):
        print(f"  {type_anomalie:<18} {nombre}")

    sorties = repertoire(cfg, "sorties")
    chemin = sorties / f"qc_{dt.date.today().isoformat()}.csv"
    with chemin.open("w", encoding="utf-8", newline="") as flux:
        writer = csv.writer(flux)
        writer.writerow(["cle_zotero", "type", "titre", "detail", "correction_doi"])
        for anomalie in anomalies:
            writer.writerow([anomalie.cle, anomalie.type, anomalie.titre,
                             anomalie.detail, anomalie.correction_doi or ""])
    print(f"\nRapport : {chemin}")

    if not plans:
        print("Aucune écriture nécessaire.")
        return CODE_OK

    print(f"\n{len(plans)} référence(s) à modifier :")
    for plan in plans[:20]:
        actions = []
        if plan.tags_ajoutes:
            actions.append("+" + ", +".join(plan.tags_ajoutes))
        if plan.tags_retires:
            actions.append("−" + ", −".join(plan.tags_retires))
        if plan.doi:
            actions.append(f"DOI ← {plan.doi}")
        print(f"  {plan.cle}  {plan.titre[:52]:<52}  {' | '.join(actions)}")
    if len(plans) > 20:
        print(f"  … et {len(plans) - 20} autres (voir le rapport CSV).")

    if not args.ecrire:
        print("\nSimulation — aucune écriture. Relancer avec --ecrire pour appliquer.")
        return CODE_OK

    modifiees, erreurs = appliquer(client, plans)
    print(f"\n{modifiees} référence(s) modifiée(s) dans Zotero.")
    for erreur in erreurs:
        print(f"  échec : {erreur}", file=sys.stderr)
    _consigner(cfg, {
        "date": dt.datetime.now().isoformat(timespec="seconds"),
        "commande": "qc",
        "modifiees": modifiees,
        "erreurs": len(erreurs),
    })
    return CODE_OK if not erreurs else CODE_ERREUR


def cmd_selectionner(args, cfg: dict) -> int:
    aujourdhui = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    nom_phase, phase = phase_active(cfg, aujourdhui)
    journal.info("Phase %s — α=%s β=%s γ=%s", nom_phase,
                 phase.get("alpha"), phase.get("beta"), phase.get("gamma"))

    client = _client(cfg)
    references = client.references()
    fiches = inventaire(cfg)
    dois_traites, cles_traitees = index_traites(fiches)

    lues = [r for r in references
            if (r.doi and r.doi in dois_traites) or r.cle in cles_traitees]
    cles_lues = {r.cle for r in lues}
    vivier = [r for r in references if r.cle not in cles_lues]

    filtre = phase.get("filtre_tags")
    if filtre:
        avant = len(vivier)
        vivier = [r for r in vivier if r.a_tag(filtre)]
        journal.info("Filtre dur de phase (%s) : %d → %d candidats.",
                     ", ".join(filtre), avant, len(vivier))
    if not vivier:
        journal.warning("Vivier vide : rien à sélectionner.")
        return CODE_OK

    cache = Cache(Path(cfg["chemins"]["cache"]) / "cache.sqlite")
    try:
        dois = [r.doi for r in vivier if r.doi]
        donnees = citations(dois, cfg, cache, sans_reseau=args.sans_reseau)
        candidats = calculer(vivier, donnees, lues, phase, cfg, aujourdhui)

        # L'effort de lecture n'est estimé que pour la tête de classement : ouvrir
        # tous les PDF du vivier chaque nuit serait un coût inutile.
        efforts: dict[str, float] = {}
        pdfs: dict[str, Path] = {}
        pages_par_unite = cfg["selection"]["pages_par_unite"]
        for candidat in candidats[:30]:
            cle = candidat.reference.cle
            chemin = client.chemin_pdf(cle)
            if chemin:
                pdfs[cle] = chemin
            pages = cache.pages(cle)
            if pages is None and chemin:
                pages = nombre_pages(chemin)
                cache.ecrire_pages(cle, pages)
            if pages:
                efforts[cle] = max(pages / pages_par_unite, 0.25)

        retenus = selectionner(candidats, cfg, efforts)
    finally:
        cache.fermer()

    sorties = repertoire(cfg, "sorties")
    horodatage = aujourdhui.isoformat()
    chemin_csv = sorties / f"classement_{horodatage}.csv"
    with chemin_csv.open("w", encoding="utf-8", newline="") as flux:
        writer = csv.DictWriter(flux, fieldnames=COLONNES)
        writer.writeheader()
        for rang, candidat in enumerate(candidats, 1):
            reference = candidat.reference
            writer.writerow({
                "rang": rang,
                "score": round(candidat.score, 4),
                "cle_zotero": reference.cle,
                "titre": reference.titre,
                "annee": reference.annee or "",
                "doi": reference.doi or "",
                "type": reference.type_zotero,
                "tags": ";".join(reference.tags),
                "citations": "" if candidat.citations is None else candidat.citations,
                "citations_imputees": int(candidat.citations_imputees),
                "age": "" if candidat.age is None else round(candidat.age, 2),
                "demi_vie": candidat.demi_vie,
                "p_impact": round(candidat.p_impact, 4),
                "p_fraicheur": round(candidat.p_fraicheur, 4),
                "p_theme": round(candidat.p_theme, 4),
                "bonus_survey": round(candidat.bonus_survey, 4),
                "urgence": round(candidat.urgence, 4),
                "redondance": round(candidat.redondance, 4),
                "effort": efforts.get(reference.cle, ""),
                "pdf": str(pdfs.get(reference.cle, "")),
                "cle_bbt": reference.cle_bbt or "",
            })

    rangs = {id(c): i for i, c in enumerate(candidats, 1)}
    tampon = repertoire(cfg, "tampon_pdf")
    selection = []
    for candidat in retenus:
        reference = candidat.reference
        chemin_pdf = pdfs.get(reference.cle) or client.chemin_pdf(reference.cle)
        destination = None
        if chemin_pdf and not args.simuler:
            destination = tampon / f"{reference.cle}_{chemin_pdf.name}"
            shutil.copy2(chemin_pdf, destination)
        selection.append({
            "titre": reference.titre,
            "annee": reference.annee,
            "doi": reference.doi,
            "cle_bbt": reference.cle_bbt,
            "pdf": str(chemin_pdf) if chemin_pdf else None,
            "pdf_tampon": str(destination) if destination else None,
            # Bloc à recopier tel quel dans la fiche de lecture (§0.3) : sans lui,
            # aucune évaluation rétrospective des algorithmes n'est possible.
            "selection": {
                "algo": VERSION_ALGO,
                "version": __version__,
                "phase": nom_phase,
                "date_selection": horodatage,
                "zotero_key": reference.cle,
                "score": round(candidat.score, 4),
                "rang": rangs[id(candidat)],
                "vivier": len(candidats),
                "poids": {"alpha": phase.get("alpha"), "beta": phase.get("beta"),
                          "gamma": phase.get("gamma")},
                "features": candidat.variables(),
            },
        })

    chemin_selection = sorties / f"selection_{horodatage}.json"
    chemin_selection.write_text(
        json.dumps(selection, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    commande = cfg["selection"].get("commande_transfert")
    transferts = []
    for entree in selection:
        if not commande or not entree["pdf_tampon"] or args.simuler:
            continue
        ligne = commande.format(pdf=shlex.quote(entree["pdf_tampon"]))
        try:
            subprocess.run(ligne, shell=True, check=True, capture_output=True, timeout=300)
            transferts.append({"pdf": entree["pdf_tampon"], "etat": "ok"})
        except Exception as err:
            journal.warning("Transfert en échec pour %s : %s — le PDF reste dans le tampon.",
                            entree["pdf_tampon"], err)
            transferts.append({"pdf": entree["pdf_tampon"], "etat": "echec"})

    _consigner(cfg, {
        "date": dt.datetime.now().isoformat(timespec="seconds"),
        "commande": "selectionner",
        "algo": VERSION_ALGO,
        "phase": nom_phase,
        "poids": {"alpha": phase.get("alpha"), "beta": phase.get("beta"),
                  "gamma": phase.get("gamma")},
        "corpus": len(references),
        "lues": len(lues),
        "vivier": len(candidats),
        "sans_reseau": bool(args.sans_reseau),
        "simulation": bool(args.simuler),
        "retenus": [
            {"cle": e["selection"]["zotero_key"], "doi": e["doi"],
             "score": e["selection"]["score"], "rang": e["selection"]["rang"]}
            for e in selection
        ],
        "transferts": transferts,
    })

    etoiles = pertinences_par_doi(fiches)
    print(f"Phase {nom_phase} — {len(references)} références, {len(lues)} traitées, "
          f"{len(candidats)} candidats" + (f", {len(etoiles)} fiches notées" if etoiles else ""))
    print(f"Classement : {chemin_csv}")
    for entree in selection:
        marque = "" if entree["pdf"] else "   [PDF absent]"
        print(f"  #{entree['selection']['rang']}  {entree['selection']['score']:.3f}  "
              f"{entree['titre'][:60]}{marque}")
    if args.simuler:
        print("Simulation — aucun PDF copié, aucun transfert.")
    return CODE_OK


# ---------------------------------------------------------------------- point d'entrée


def construire_analyseur() -> argparse.ArgumentParser:
    analyseur = argparse.ArgumentParser(
        prog="selecteur",
        description="Sélection algorithmique des lectures quotidiennes (thèse CTI).",
    )
    analyseur.add_argument("--config", help="chemin d'un fichier de configuration")
    analyseur.add_argument("-v", "--verbose", action="store_true", help="journal détaillé")
    analyseur.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sous = analyseur.add_subparsers(dest="commande", required=True)

    sous.add_parser("verifier", help="diagnostic de l'installation")
    sous.add_parser("autoriser", help="obtenir une clé d'API locale Zotero")
    sous.add_parser("exporter", help="exporter le corpus normalisé")

    p_qc = sous.add_parser("qc", help="contrôle qualité de la bibliographie")
    p_qc.add_argument("--ecrire", action="store_true",
                      help="appliquer les corrections dans Zotero (défaut : simulation)")

    p_pdf = sous.add_parser("pdf", help="chemin du PDF d'une référence")
    p_pdf.add_argument("cle", help="clé Zotero de la référence")

    p_sel = sous.add_parser("selectionner", help="chaîne nocturne de sélection")
    p_sel.add_argument("--date", help="date de référence (AAAA-MM-JJ), pour rejouer une nuit")
    p_sel.add_argument("--sans-reseau", action="store_true",
                       help="n'utiliser que le cache OpenAlex")
    p_sel.add_argument("--simuler", action="store_true",
                       help="calculer et classer sans copier ni transférer de PDF")
    return analyseur


def main(argv: list[str] | None = None) -> int:
    args = construire_analyseur().parse_args(argv)
    try:
        cfg = charger(args.config)
    except ErreurConfig as err:
        print(f"Configuration : {err}", file=sys.stderr)
        return CODE_ERREUR

    _configurer_journal(cfg, args.verbose)
    commandes = {
        "verifier": cmd_verifier,
        "autoriser": cmd_autoriser,
        "exporter": cmd_exporter,
        "qc": cmd_qc,
        "pdf": cmd_pdf,
        "selectionner": cmd_selectionner,
    }
    try:
        return commandes[args.commande](args, cfg)
    except ZoteroIndisponible as err:
        print(f"Zotero : {err}", file=sys.stderr)
        return CODE_ZOTERO
    except AutorisationRequise as err:
        print(f"Autorisation : {err}", file=sys.stderr)
        return CODE_ERREUR
    except ErreurConfig as err:
        print(f"Configuration : {err}", file=sys.stderr)
        return CODE_ERREUR
    except KeyboardInterrupt:
        return CODE_ERREUR


if __name__ == "__main__":
    sys.exit(main())
