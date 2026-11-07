"""
sspcloud_mcp.validation — Validation des arguments qui finissent dans un chemin
ou une commande shell.

Un argument venu de l'agent ne doit jamais etre interprete par un shell : on le
valide ici (liste blanche de caracteres), puis les appelants le citent avec
shlex.quote. Les deux gestes sont necessaires : la validation donne un refus
lisible a l'agent, le quoting garde la commande saine meme si la validation
evolue.
"""

from __future__ import annotations

import re
import shlex

from .errors import MCPToolError

_NOM_PROJET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}"
_DESTINATION = re.compile(rf"^{_SEGMENT}(/{_SEGMENT}){{0,7}}$")
_REFERENCE_GIT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_URL_GIT = re.compile(r"^[A-Za-z0-9@:/._~%+=?&#-]{1,500}$")


def argument_invalide(nom: str, valeur, attendu: str) -> MCPToolError:
    return MCPToolError(
        "INVALID_ARGUMENT",
        f"Argument {nom!r} refuse : {str(valeur)[:80]!r}.",
        f"Attendu : {attendu}",
    )


def nom_projet(valeur) -> str:
    """Nom de projet : lettres, chiffres, point, tiret, souligne ; 63 caracteres au plus."""
    if (not isinstance(valeur, str) or not _NOM_PROJET.match(valeur)
            or ".." in valeur):
        raise argument_invalide(
            "project", valeur,
            "lettres, chiffres, '.', '-' et '_' (63 caracteres au plus, sans espace "
            "ni '..'), ex. 'claris-open'.")
    return valeur


def destination(valeur) -> str:
    """Dossier de destination relatif au dossier de travail : jamais absolu, jamais '..'."""
    if (not isinstance(valeur, str) or not _DESTINATION.match(valeur)
            or any(s in (".", "..") for s in valeur.split("/"))):
        raise argument_invalide(
            "dest", valeur,
            "un chemin relatif au dossier de travail, sans espace, sans '..', ex. "
            "'mon-repo' ou 'org/mon-repo'.")
    return valeur


def reference_git(valeur) -> str:
    """Nom de branche ou d'etiquette."""
    if not isinstance(valeur, str) or not _REFERENCE_GIT.match(valeur) or ".." in valeur:
        raise argument_invalide(
            "branch", valeur, "un nom de branche git, ex. 'main' ou 'feature/x'.")
    return valeur


def url_git(valeur) -> str:
    if not isinstance(valeur, str) or not _URL_GIT.match(valeur) or valeur.startswith("-"):
        raise argument_invalide(
            "source", valeur, "une URL git sans espace ni caractere de controle.")
    return valeur


def chemin_pour_cd(chemin: str) -> str:
    """Fragment shell sur pour `cd` : cite le chemin, en gardant le `~` developpe."""
    if chemin == "~":
        return '"$HOME"'
    if chemin.startswith("~/"):
        return '"$HOME"/' + shlex.quote(chemin[2:])
    return shlex.quote(chemin)
