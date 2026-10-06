# Sélecteur de lectures — thèse CTI

Implémentation des familles **A** (score additif raffiné) et **E** (profils de phase) du
document de conception, avec l'instrumentation du §0.3 et le contrôle qualité de la
bibliographie. Accès à Zotero par l'**API locale** (lecture sans authentification, écriture
par clé locale), sur Ubuntu.

```
score = α·p(impact) + β·p(fraîcheur) + γ·p(thème) + bonus_survey + urgence − ρ·redondance
```

où `p(·)` est le rang-percentile du terme sur le vivier de la nuit.

---

## 1. Prérequis

- **Zotero 10 ou supérieur** — l'écriture par l'API locale n'existe pas avant. Vérifier
  dans Zotero : *Aide > À propos*.
- Dans Zotero : **Paramètres > Avancé > « Autoriser les autres applications de cet
  ordinateur à communiquer avec Zotero »**.
- Python 3.11+.

> **Zotero doit être lancé** pour que l'API locale réponde. C'est la différence majeure
> avec la lecture d'une copie de `zotero.sqlite`, qui fonctionnait application fermée.
> Voir §6 pour les conséquences sur la tâche planifiée.

## 2. Installation

```bash
sudo apt install python3-venv
mkdir -p ~/these/selecteur && cd ~/these/selecteur
python3 -m venv .venv && source .venv/bin/activate
pip install -r /chemin/vers/selecteur-cti/requirements.txt
cp -r /chemin/vers/selecteur-cti/selecteur ~/these/selecteur/
```

Configuration :

```bash
mkdir -p ~/.config/selecteur-cti
cp /chemin/vers/selecteur-cti/config.example.yaml ~/.config/selecteur-cti/config.yaml
$EDITOR ~/.config/selecteur-cti/config.yaml     # chemins, mailto OpenAlex, dates de phase
```

Diagnostic :

```bash
python3 -m selecteur verifier
```

Cette commande affiche la phase active, l'état des répertoires, la présence de `pypdf`,
la joignabilité de Zotero et le nombre de fiches appariables. Code de retour `2` si
Zotero est injoignable — utile pour un `||` dans le script de cron.

## 3. Autorisation d'écriture (une seule fois)

```bash
python3 -m selecteur autoriser
```

Zotero affiche une boîte de dialogue. **Choisir « Toujours autoriser »** : « Autoriser »
accorde une clé à usage unique, consommée par la première écriture, ce qui rendrait la
tâche planifiée inopérante dès le lendemain. La clé persistante est écrite dans
`~/.config/selecteur-cti/local-api-key.json` en permissions `600`, avec l'identifiant du
serveur Zotero associé.

Si la clé est révoquée (Zotero, *Paramètres*) ou la base restaurée depuis une sauvegarde,
la commande est simplement à relancer. Le script le signale explicitement plutôt que
d'échouer en silence.

La lecture, elle, ne demande aucune autorisation : `verifier`, `exporter` et
`selectionner` fonctionnent sans clé. Seul `qc --ecrire` en a besoin.

## 4. Commandes

| Commande | Effet |
|---|---|
| `verifier` | diagnostic complet de l'installation |
| `autoriser` | obtient la clé d'API locale |
| `exporter` | exporte le corpus normalisé (CSV + JSON), avec la colonne `traite` |
| `pdf CLE` | affiche le chemin sur disque du PDF d'une référence |
| `qc` | contrôle qualité — **simulation par défaut** |
| `qc --ecrire` | applique les corrections dans Zotero |
| `selectionner` | chaîne nocturne complète |

Options utiles de `selectionner` :

- `--simuler` — calcule et classe sans copier ni transférer de PDF ;
- `--sans-reseau` — n'utilise que le cache OpenAlex (mode dégradé assumé) ;
- `--date AAAA-MM-JJ` — rejoue une nuit passée, pour l'évaluation rétrospective du §F.1.

## 5. Ce que le script écrit dans Zotero

Uniquement via `qc --ecrire`, et uniquement :

- des tags préfixés `qc:` (`qc:doi-manquant`, `qc:doublon-doi`, `qc:sans-pdf`), posés
  **et retirés** selon l'état courant de la bibliographie ;
- le champ DOI, lorsqu'un DOI valide figure dans `extra` ou dans l'URL mais pas dans le
  champ dédié.

Ce que le script ne fait jamais : fusionner des doublons, supprimer un élément, modifier
un tag qui n'est pas préfixé `qc:`, ou toucher aux pièces jointes. Les doublons de titre à
DOI distincts sont signalés dans le rapport, pas tagués : ils appellent une vérification
humaine. Le type de chaque tag existant (manuel ou automatique) est préservé.

## 6. Tâche planifiée

L'appel étant un simple HTTP vers `localhost`, **aucune variable `DISPLAY` ni session
graphique n'est nécessaire côté script** — mais Zotero doit tourner dans la session.

```cron
# ~/these/selecteur/nuit.sh, exécuté chaque nuit à 3 h 10
10 3 * * * /home/loic/these/selecteur/nuit.sh >> /home/loic/these/selecteur/sorties/cron.log 2>&1
```

```bash
#!/usr/bin/env bash
# nuit.sh
set -euo pipefail
cd /home/loic/these/selecteur
source .venv/bin/activate
python3 -m selecteur selectionner || {
    code=$?
    [ "$code" -eq 2 ] && echo "Zotero fermé — sélection reportée." && exit 0
    exit "$code"
}
```

Le code de retour `2` (Zotero injoignable) est traité comme un report, non comme une
erreur : une nuit sans sélection ne doit pas faire échouer la chaîne ni noyer la boîte
mail de cron. Si la session est fermée la nuit, deux options : garder Zotero ouvert, ou
déplacer l'exécution en début de matinée par un *timer* `systemd --user` avec
`Persistent=true`, qui rattrape l'exécution manquée au déverrouillage.

## 7. Sorties

Dans `chemins.sorties` :

- `classement_AAAA-MM-JJ.csv` — **tout** le vivier, trié, avec chaque variable ayant pesé
  dans le score. C'est la pièce auditable : aucun rang n'est opaque.
- `selection_AAAA-MM-JJ.json` — les lectures du jour, chacune accompagnée d'un bloc
  `selection` prêt à être recopié dans la fiche de lecture.
- `journal.jsonl` — une ligne par exécution : algorithme, version, phase, pondérations,
  taille du vivier, articles retenus, état des transferts.
- `selecteur.log` — journal technique.

Les PDF retenus sont copiés dans `chemins.tampon_pdf`, puis transmis par
`selection.commande_transfert` si elle est configurée (`rmapi put {pdf} /Lectures`). En cas
d'échec du transfert, les PDF restent dans le tampon pour un envoi manuel au matin.

### Le bloc `selection`

À recopier dans la fiche de lecture, au premier niveau du JSON :

```json
"selection": {
  "algo": "A+E-2026.09",
  "phase": "positionnement",
  "date_selection": "2026-09-10",
  "zotero_key": "K2",
  "score": 3.25,
  "rang": 1,
  "vivier": 412,
  "poids": {"alpha": 1.0, "beta": 1.5, "gamma": 2.5},
  "features": {"impact": 0.5, "fraicheur": 1.0, "theme": 0.5, "citations_imputees": true}
}
```

Sans lui, aucune évaluation rétrospective des algorithmes (§F.1) n'est possible : c'est ce
bloc qui transforme chaque fiche en observation exploitable. Son champ `zotero_key` fournit
en outre un appariement de secours pour les documents sans DOI — rapports, thèses,
littérature grise francophone — sans jamais se substituer au DOI quand il existe.

## 8. Réglages les plus structurants

| Réglage | Effet |
|---|---|
| `phases.*.{alpha,beta,gamma}` | profil de la phase de thèse (§E.1) |
| `scoring.demi_vie_par_tag` | vitesse d'obsolescence par littérature (§A.2) |
| `scoring.redondance` | pénalité de similarité aux articles déjà lus (§A.5) |
| `selection.mode: budget` | cadence à effort constant plutôt qu'à nombre fixe (§B.3) |
| `echeance.{date,tags}` | mode piloté par échéance de rédaction (§E.2) |
| `phases.*.filtre_tags` | filtre dur : ne lire que ce qui sert le chapitre courant |

Le mode `budget` suppose `pypdf` installé, faute de quoi tous les articles reçoivent
`selection.effort_defaut` et le mode se comporte comme le mode cardinal.

## 9. Tests

```bash
python3 -m unittest discover -s tests -v
```

Dix-neuf tests, sans Zotero ni réseau : normalisation des DOI, décroissance de fraîcheur,
amortissement de l'impact, bonus de pont, résolution des phases, imputation OpenAlex,
pénalité de redondance, modes de sélection, inventaire des fiches, détection et
planification du contrôle qualité.

## 10. Limites connues

- **Zotero doit tourner.** Contrainte de l'API locale, sans contournement propre.
- **Les versions locales ne sont pas celles de l'API web.** Elles dépendent du
  `server_id` ; le client le vérifie et refuse de mélanger deux bases plutôt que de
  produire des écritures silencieusement erronées.
- **OpenAlex couvre mal la littérature grise et francophone.** Les citations manquantes
  sont imputées par la médiane de la même année et marquées `citations_imputees` dans le
  CSV — jamais remplacées par un zéro silencieux, qui condamnerait ces documents au bas
  du classement.
- **La redondance se calcule sur titre, revue et résumé.** Quand les résumés manquent dans
  Zotero, le signal s'appauvrit. Le couplage bibliographique de la famille C y remédiera.
- **Aucune famille B, C ou D n'est implémentée**, hormis le budget d'effort (B.3) et le MMR
  (`selection.diversite`), tous deux désactivables. C'est délibéré : la feuille de route
  place leur adoption après l'évaluation rétrospective de la famille A.
