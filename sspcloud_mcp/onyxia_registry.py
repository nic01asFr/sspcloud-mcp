"""
sspcloud_mcp.onyxia_registry — Visibilité des pods lancés par le service dans
Onyxia (« Mes services »).

Onyxia ne liste pas les releases Helm : il liste les Secrets
`sh.onyxia.release.v1.<release>` (type `onyxia.sh/release.v1`) du namespace de
l'utilisateur. Sans ce Secret, une release lancée en ligne de commande est
invisible bien que `helm list` la voie. Le service le crée donc après chaque
`helm install` (même convention que charts/sspcloud-mcp/scripts/install.sh) et
le supprime au `helm uninstall`.

Le Secret sert aussi de marqueur de propriété : `catalog` vaut CATALOG pour les
releases lancées par le service, qui seules peuvent être désinstallées par lui.
"""

from __future__ import annotations

import base64
import os
import re

from . import transports as T

CATALOG = "sspcloud-mcp"
_PREFIX = "sh.onyxia.release.v1."
# Pod StatefulSet d'un chart interactif : {release}-{chart}-0
_POD_RE = re.compile(r"^(?P<release>.+)-(?P<chart>jupyter-python|jupyter-pytorch-gpu)-0$")


def secret_name(release: str) -> str:
    return f"{_PREFIX}{release}"


def owner_of(namespace: str) -> str:
    """idep Onyxia : ONYXIA_USER ou namespace `user-<idep>` sans le préfixe."""
    user = os.getenv("ONYXIA_USER", "")
    if user:
        return user
    return namespace[len("user-"):] if namespace.startswith("user-") else namespace


def release_of_pod(pod: str) -> str | None:
    """Nom de la release Helm d'un pod de chart interactif, sinon None."""
    m = _POD_RE.match(pod)
    return m.group("release") if m else None


def register(release: str, namespace: str, friendly_name: str = "") -> bool:
    """Crée (ou met à jour) le Secret Onyxia. Non bloquant : False si refusé."""
    rc, yml, _ = T.kubectl(
        "create", "secret", "generic", secret_name(release),
        "--type=onyxia.sh/release.v1",
        f"--from-literal=owner={owner_of(namespace)}",
        f"--from-literal=friendlyName={friendly_name or release}",
        f"--from-literal=catalog={CATALOG}",
        "--from-literal=share=false",
        "--dry-run=client", "-o", "yaml", namespace=namespace, timeout=20)
    if rc != 0 or not yml.strip():
        return False
    rc, _, _ = T.kubectl("apply", "-f", "-", namespace=namespace,
                         timeout=20, input_text=yml)
    return rc == 0


def is_managed(release: str, namespace: str) -> bool:
    """True si la release a été lancée par le service (marqueur catalog)."""
    rc, out, _ = T.kubectl(
        "get", "secret", secret_name(release), "-o",
        "jsonpath={.data.catalog}", namespace=namespace, timeout=15)
    if rc != 0 or not out.strip():
        return False
    try:
        return base64.b64decode(out.strip()).decode() == CATALOG
    except Exception:
        return False


_URL_RE = re.compile(r"https?://[^\s)>\]\"']+")
_PWD_RE = re.compile(r"(?im)^(\s*password\s*:\s*)(\S+)")
_NOTES_MAX = 3000


def parse_notes(text: str, reveal: bool = False) -> dict:
    """Résume le NOTES.txt d'une release : URLs d'accès, mot de passe, texte.

    Convention Onyxia : la première URL est le bouton Ouvrir, la ligne
    `password: <valeur>` le bouton Copier. Le mot de passe est masqué dans le
    texte et `password` sauf reveal=True.
    """
    urls: list[str] = []
    for u in _URL_RE.findall(text):
        u = u.rstrip(".,;")
        if u not in urls:
            urls.append(u)
    m = _PWD_RE.search(text)
    password = m.group(2) if m else ""
    shown = text if reveal else _PWD_RE.sub(r"\1********", text)
    out = {"open_url": urls[0] if urls else "", "urls": urls,
           "has_password": bool(password),
           "text": shown[:_NOTES_MAX] + ("…" if len(shown) > _NOTES_MAX else "")}
    if reveal and password:
        out["password"] = password
    return out


def notes(release: str, namespace: str, reveal: bool = False) -> dict:
    """NOTES.txt de la release Helm (`helm get notes`), résumé par parse_notes."""
    import subprocess
    from ._helm import find_helm, helm_env
    try:
        r = subprocess.run([find_helm(), "get", "notes", release, "-n", namespace],
                           capture_output=True, text=True, timeout=30,
                           env=helm_env())
    except Exception as e:
        return {"available": False, "reason": str(e)[:150]}
    if r.returncode != 0 or not r.stdout.strip():
        return {"available": False, "reason": (r.stderr or "release sans notes").strip()[:150]}
    # `helm get notes` préfixe la sortie par "NOTES:"
    body = r.stdout.split("\n", 1)[1] if r.stdout.startswith("NOTES:") else r.stdout
    return {"available": True, **parse_notes(body, reveal)}


def unregister(release: str, namespace: str) -> bool:
    rc, _, _ = T.kubectl("delete", "secret", secret_name(release),
                         "--ignore-not-found", namespace=namespace, timeout=20)
    return rc == 0
