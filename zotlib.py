"""Accès à Zotero par l'API locale (Zotero 10+), en lecture et en écriture.

Lecture : aucune authentification. Écriture : clé d'API locale accordée par l'utilisateur
via une boîte de dialogue Zotero ; « Toujours autoriser » produit une clé persistante que
l'on stocke pour les exécutions ultérieures, notamment sous cron.

Prérequis côté Zotero : Paramètres > Avancé > « Autoriser les autres applications de cet
ordinateur à communiquer avec Zotero ». Zotero doit être lancé.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

try:
    from pyzotero import (
        LocalAPIDeniedError,
        LocalAPIKeyRequiredError,
        ServerIDMismatchError,
        Zotero,
    )
except ImportError as err:  # pyzotero antérieur au support de l'écriture locale
    raise ImportError(
        "pyzotero ≥ 1.15.1 est requis pour l'écriture par l'API locale "
        "(pip install --upgrade pyzotero)."
    ) from err

from .config import CHEMIN_CLE

journal = logging.getLogger("selecteur.zotero")

TYPES_IGNORES = {"attachment", "note", "annotation"}
_RE_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>]+)", re.IGNORECASE)
_RE_ANNEE = re.compile(r"\b(1[5-9]\d{2}|20\d{2})\b")
_RE_CLE_BBT = re.compile(r"^Citation Key\s*:\s*(\S+)", re.IGNORECASE | re.MULTILINE)


class ZoteroIndisponible(Exception):
    """Zotero n'est pas lancé, ou son API locale n'est pas activée."""


class AutorisationRequise(Exception):
    """Aucune clé d'API locale valide : lancer `selecteur autoriser`."""


def normaliser_doi(valeur: str | None) -> str | None:
    """Extrait et normalise un DOI depuis un DOI brut, une URL ou un champ libre."""
    if not valeur:
        return None
    trouve = _RE_DOI.search(str(valeur))
    if not trouve:
        return None
    doi = trouve.group(1).rstrip(" .,;)/")
    return doi.lower()


@dataclass
class Reference:
    """Une référence Zotero normalisée pour le reste de la chaîne."""

    cle: str
    titre: str
    annee: int | None
    doi: str | None                       # normalisé, minuscules
    doi_brut: str | None
    tags: list[str] = field(default_factory=list)
    type_zotero: str = ""
    publication: str = ""
    resume: str = ""
    extra: str = ""
    cle_bbt: str | None = None
    version: int | None = None
    date_ajout: str = ""

    def a_tag(self, tags: Iterable[str]) -> bool:
        ensemble = {t.lower() for t in self.tags}
        return any(t.lower() in ensemble for t in tags)


def _chemin_json(donnees: dict, chemin: str) -> Any:
    courant: Any = donnees
    for morceau in chemin.split("."):
        if not isinstance(courant, dict) or morceau not in courant:
            return None
        courant = courant[morceau]
    return courant


def _annee(donnees: dict) -> int | None:
    for source in (donnees.get("date"), (donnees.get("meta") or {}).get("parsedDate")):
        if not source:
            continue
        trouve = _RE_ANNEE.search(str(source))
        if trouve:
            return int(trouve.group(1))
    return None


class ClientZotero:
    """Enveloppe pyzotero : lecture sans authentification, écriture avec clé locale."""

    def __init__(self, cfg: dict, chemin_cle: Path | None = None) -> None:
        self.cfg = cfg
        self.chemin_cle = chemin_cle or CHEMIN_CLE
        self._identifiants = self._lire_cle()
        self._client: Zotero | None = None

    # ------------------------------------------------------------------ clé

    def _lire_cle(self) -> dict:
        depuis_env = os.environ.get("SELECTEUR_LOCAL_API_KEY")
        if depuis_env:
            return {"key": depuis_env, "server_id": os.environ.get("SELECTEUR_SERVER_ID")}
        if self.chemin_cle.exists():
            try:
                return json.loads(self.chemin_cle.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                journal.warning("Fichier de clé illisible : %s", self.chemin_cle)
        return {}

    def _ecrire_cle(self, cle: str, server_id: str | None) -> None:
        self.chemin_cle.parent.mkdir(parents=True, exist_ok=True)
        contenu = {
            "key": cle,
            "server_id": server_id,
            "app_name": self.cfg["zotero"]["app_name"],
        }
        self.chemin_cle.write_text(json.dumps(contenu, indent=2), encoding="utf-8")
        os.chmod(self.chemin_cle, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        self._identifiants = contenu

    @property
    def a_cle(self) -> bool:
        return bool(self._identifiants.get("key"))

    # --------------------------------------------------------------- client

    @property
    def client(self) -> Zotero:
        if self._client is None:
            self._client = Zotero(
                str(self.cfg["zotero"]["library_id"]),
                self.cfg["zotero"]["library_type"],
                local=True,
                server_id=self._identifiants.get("server_id"),
                local_api_key=self._identifiants.get("key"),
            )
        return self._client

    def verifier(self) -> dict:
        """Teste la joignabilité de l'API locale. Lève ZoteroIndisponible sinon."""
        try:
            nombre = self.client.count_items()
        except ServerIDMismatchError as err:
            raise ZoteroIndisponible(
                "L'identifiant de serveur enregistré ne correspond pas à cette base Zotero. "
                "La base a probablement été restaurée ou remplacée : relancer « selecteur autoriser »."
            ) from err
        except Exception as err:  # httpx, connexion refusée, 404…
            raise ZoteroIndisponible(
                "API locale injoignable. Vérifier que Zotero est lancé et que l'option "
                "Paramètres > Avancé > « Autoriser les autres applications… » est cochée."
            ) from err
        return {"items": nombre, "server_id": getattr(self.client, "server_id", None)}

    def autoriser(self) -> dict:
        """Demande une clé d'API locale ; Zotero affiche une boîte de dialogue."""
        try:
            reponse = self.client.authorize_local(self.cfg["zotero"]["app_name"])
        except LocalAPIDeniedError as err:
            raise AutorisationRequise("Autorisation refusée dans Zotero.") from err
        if reponse.get("remember"):
            self._ecrire_cle(reponse["key"], getattr(self.client, "server_id", None))
        return reponse

    # -------------------------------------------------------------- lecture

    def references(self) -> list[Reference]:
        """Toutes les références bibliographiques de premier niveau, normalisées."""
        collection = self.cfg["zotero"].get("collection")
        try:
            if collection:
                bruts = self.client.everything(
                    self.client.collection_items_top(collection, limit=100)
                )
            else:
                bruts = self.client.everything(self.client.top(limit=100))
        except Exception as err:
            raise ZoteroIndisponible(f"Lecture de la bibliothèque impossible : {err}") from err

        refs: list[Reference] = []
        for brut in bruts:
            donnees = brut.get("data", {})
            type_zotero = donnees.get("itemType", "")
            if type_zotero in TYPES_IGNORES:
                continue
            extra = donnees.get("extra", "") or ""
            doi_brut = donnees.get("DOI") or None
            doi = normaliser_doi(doi_brut) or normaliser_doi(extra) or normaliser_doi(
                donnees.get("url")
            )
            cle_bbt = _RE_CLE_BBT.search(extra)
            refs.append(
                Reference(
                    cle=donnees.get("key", ""),
                    titre=(donnees.get("title") or "").strip(),
                    annee=_annee({**donnees, "meta": brut.get("meta", {})}),
                    doi=doi,
                    doi_brut=doi_brut,
                    tags=[t.get("tag", "") for t in donnees.get("tags", []) if t.get("tag")],
                    type_zotero=type_zotero,
                    publication=(
                        donnees.get("publicationTitle")
                        or donnees.get("proceedingsTitle")
                        or donnees.get("repository")
                        or ""
                    ),
                    resume=(donnees.get("abstractNote") or "")[:4000],
                    extra=extra,
                    cle_bbt=cle_bbt.group(1) if cle_bbt else None,
                    version=donnees.get("version"),
                    date_ajout=donnees.get("dateAdded", ""),
                )
            )
        journal.info("%d références lues depuis Zotero.", len(refs))
        return refs

    # ------------------------------------------------------------------ PDF

    def _url_locale(self, suffixe: str) -> str:
        base = self.cfg["zotero"]["base_url"].rstrip("/")
        prefixe = (
            f"users/{self.cfg['zotero']['library_id']}"
            if self.cfg["zotero"]["library_type"] == "user"
            else f"groups/{self.cfg['zotero']['library_id']}"
        )
        return f"{base}/{prefixe}/{suffixe}"

    def _url_fichier(self, cle_piece: str) -> str | None:
        """Interroge /items/<clé>/file/view/url, qui renvoie une URL file:// en texte brut."""
        url = self._url_locale(f"items/{cle_piece}/file/view/url")
        try:
            with urllib.request.urlopen(url, timeout=self.cfg["zotero"]["timeout"]) as reponse:
                return reponse.read().decode("utf-8").strip()
        except Exception as err:
            journal.debug("Endpoint file/view/url indisponible pour %s : %s", cle_piece, err)
            return None

    def _chemin_depuis_piece(self, piece: dict) -> Path | None:
        """Repli : reconstruction du chemin depuis les métadonnées de la pièce jointe."""
        donnees = piece.get("data", {})
        mode = donnees.get("linkMode", "")
        chemin = donnees.get("path") or ""
        if mode == "linked_file" and chemin:
            if chemin.startswith("attachments:"):
                base = self.cfg["zotero"].get("repertoire_liens")
                if not base:
                    return None
                return Path(base).expanduser() / chemin[len("attachments:"):]
            return Path(chemin).expanduser()
        nom = donnees.get("filename") or ""
        if nom:
            return Path(self.cfg["zotero"]["repertoire_storage"]) / donnees.get("key", "") / nom
        return None

    def chemin_pdf(self, cle_parente: str) -> Path | None:
        """Chemin sur disque du premier PDF attaché à une référence, ou None."""
        try:
            enfants = self.client.children(cle_parente)
        except Exception as err:
            journal.debug("Pièces jointes illisibles pour %s : %s", cle_parente, err)
            return None

        for enfant in enfants:
            donnees = enfant.get("data", {})
            if donnees.get("itemType") != "attachment":
                continue
            if donnees.get("contentType") != "application/pdf":
                continue
            url = self._url_fichier(donnees.get("key", ""))
            if url and url.startswith("file://"):
                chemin = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path))
                if chemin.exists():
                    return chemin
            repli = self._chemin_depuis_piece(enfant)
            if repli and repli.exists():
                return repli
        return None

    # ------------------------------------------------------------- écriture

    def _item(self, cle: str) -> dict:
        reponse = self.client.item(cle)
        return reponse[0] if isinstance(reponse, list) else reponse

    def modifier(
        self,
        cle: str,
        tags_ajoutes: Iterable[str] = (),
        tags_retires: Iterable[str] = (),
        doi: str | None = None,
    ) -> bool:
        """Applique en une seule écriture un ajout/retrait de tags et une pose de DOI.

        Une écriture unique par référence évite le conflit de version qu'entraînerait
        un enchaînement add_tags() puis update_item() sur le même objet.
        """
        if not self.a_cle:
            raise AutorisationRequise(
                "Aucune clé d'API locale. Lancer « selecteur autoriser » et choisir "
                "« Toujours autoriser » dans la boîte de dialogue Zotero."
            )
        item = self._item(cle)
        donnees = item["data"]
        actuels = [t for t in donnees.get("tags", []) if t.get("tag")]
        retires = {t.lower() for t in tags_retires}
        # Les entrées existantes sont conservées telles quelles : réécrire un tag manuel
        # avec "type": 1 le transformerait en tag automatique dans Zotero.
        nouveaux = [t for t in actuels if t["tag"].lower() not in retires]
        presents = {t["tag"] for t in nouveaux}
        for tag in tags_ajoutes:
            if tag not in presents:
                nouveaux.append({"tag": tag, "type": 1})  # 1 = tag automatique
                presents.add(tag)

        change = nouveaux != actuels
        if doi and donnees.get("DOI", "") != doi:
            donnees["DOI"] = doi
            change = True
        if not change:
            return False

        donnees["tags"] = nouveaux
        try:
            self.client.update_item(item)
        except LocalAPIKeyRequiredError as err:
            raise AutorisationRequise(
                "Clé d'API locale consommée ou révoquée : relancer « selecteur autoriser »."
            ) from err
        return True
