"""
sspcloud_mcp.onyxia_registry — Visibilité des pods lancés par le service dans
Onyxia (« Mes services »).

Onyxia ne liste pas les releases Helm : il liste les Secrets
`sh.onyxia.release.v1.<release>` (type `onyxia.sh/release.v1`) du namespace de
l'utilisateur. Sans ce Secret, une release lancée en ligne de commande est
invisible bien que `helm list` la voie. Le service le crée donc après chaque
`helm install` (même convention que charts/sspcloud-mcp/scripts/install.sh) et
le supprime au `helm uninstall`.

`catalog` doit être un vrai catalogue Onyxia : c'est par lui qu'Onyxia retrouve le
chart pour Suspendre/Reprendre/Mettre à jour (un catalogue inconnu donne une erreur
500 sur `/api/my-lab/app/resume`). Les charts interactifs InseeFrLab sont dans `ide`.

La propriété est portée par l'annotation `sspcloud-mcp/origin` : `launched` pour
les releases lancées par le service, qui seules peuvent être désinstallées par lui ;
`adopted` pour les releases préexistantes rendues visibles.
"""

from __future__ import annotations

import base64
import os
import re

from . import transports as T

CATALOG = "ide"                      # catalogue Onyxia des charts interactifs
# Ancien catalogue inventé des pods adoptés (cassait Suspendre/Reprendre) : à réparer.
_LEGACY_ADOPTED_CATALOG = "sspcloud-mcp-adopted"
ORIGIN_ANNOTATION = "sspcloud-mcp/origin"
ORIGIN_LAUNCHED = "launched"
ORIGIN_ADOPTED = "adopted"
# Charts interactifs InseeFrLab affichables comme un service Onyxia.
INTERACTIVE_CHARTS = ("jupyter-python", "jupyter-pytorch-gpu", "vscode-python")
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


def register(release: str, namespace: str, friendly_name: str = "",
             catalog: str = "", origin: str = ORIGIN_LAUNCHED) -> bool:
    """Crée (ou met à jour) le Secret Onyxia. Non bloquant : False si refusé."""
    rc, yml, _ = T.kubectl(
        "create", "secret", "generic", secret_name(release),
        "--type=onyxia.sh/release.v1",
        f"--from-literal=owner={owner_of(namespace)}",
        f"--from-literal=friendlyName={friendly_name or release}",
        f"--from-literal=catalog={catalog or CATALOG}",
        "--from-literal=share=false",
        "--dry-run=client", "-o", "yaml", namespace=namespace, timeout=20)
    if rc != 0 or not yml.strip():
        return False
    rc, _, _ = T.kubectl("apply", "-f", "-", namespace=namespace,
                         timeout=20, input_text=yml)
    if rc != 0:
        return False
    T.kubectl("annotate", "secret", secret_name(release),
              f"{ORIGIN_ANNOTATION}={origin}", "--overwrite",
              namespace=namespace, timeout=15)
    return True


def is_managed(release: str, namespace: str) -> bool:
    """True si la release a été lancée par le service (annotation d'origine)."""
    rc, out, _ = T.kubectl(
        "get", "secret", secret_name(release), "-o", "json",
        namespace=namespace, timeout=15)
    if rc != 0 or not out.strip():
        return False
    try:
        import json
        ann = json.loads(out)["metadata"].get("annotations") or {}
        return ann.get(ORIGIN_ANNOTATION) == ORIGIN_LAUNCHED
    except Exception:
        return False


_URL_RE = re.compile(r"https?://[^\s)>\]\"'`]+")
# « password: x », « Your password: x », « **Password**: x » (formats des charts)
_PWD_RE = re.compile(r"(?i)(password\W{0,4}\s*:\s*\W{0,2})([^\s`*]+)")
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
        if "<" in u or ">" in u:      # modèle `http://localhost:<local-port>`
            continue
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


def _helm_releases(namespace: str) -> list[dict]:
    import json
    import subprocess
    from ._helm import find_helm, helm_env
    r = subprocess.run([find_helm(), "list", "-n", namespace, "-o", "json", "--max", "500"],
                       capture_output=True, text=True, timeout=40, env=helm_env())
    return json.loads(r.stdout or "[]") if r.returncode == 0 else []


def _registered(namespace: str) -> set[str]:
    """Releases déjà visibles ET correctes. Un Secret à l'ancien catalogue inventé
    n'est pas compté : il est refait par adopt()."""
    import json
    rc, out, _ = T.kubectl("get", "secrets", "-o", "json", namespace=namespace,
                           timeout=30)
    if rc != 0 or not out.strip():
        return set()
    done: set[str] = set()
    for it in json.loads(out).get("items", []):
        name = it["metadata"]["name"]
        if not name.startswith(_PREFIX):
            continue
        try:
            cat = base64.b64decode((it.get("data") or {}).get("catalog", "")).decode()
        except Exception:
            cat = ""
        if cat != _LEGACY_ADOPTED_CATALOG:
            done.add(name[len(_PREFIX):])
    return done


def candidates(namespace: str) -> list[dict]:
    """Releases interactives déployées, absentes de « Mes services »."""
    known = _registered(namespace)
    out = []
    for r in _helm_releases(namespace):
        chart = r.get("chart", "")
        base = next((c for c in INTERACTIVE_CHARTS if chart.startswith(c + "-")), "")
        if base and r.get("status") == "deployed" and r["name"] not in known:
            out.append({"release": r["name"], "chart": base})
    return sorted(out, key=lambda x: x["release"])


def _statefulset_replicas(namespace: str) -> dict[str, int]:
    import json
    rc, out, _ = T.kubectl("get", "statefulsets", "-o", "json", namespace=namespace,
                           timeout=25)
    if rc != 0 or not out.strip():
        return {}
    return {i["metadata"]["name"]: int(i["spec"].get("replicas") or 0)
            for i in json.loads(out).get("items", [])}


def _helm_suspended(release: str, namespace: str) -> bool:
    import json
    import subprocess
    from ._helm import find_helm, helm_env
    r = subprocess.run([find_helm(), "get", "values", release, "-n", namespace,
                        "-a", "-o", "json"], capture_output=True, text=True,
                       timeout=30, env=helm_env())
    try:
        return bool(json.loads(r.stdout or "{}").get("global", {}).get("suspend"))
    except Exception:
        return False


def sync_suspension(namespace: str, releases: list[str] | None = None,
                    dry_run: bool = False) -> dict:
    """Aligne l'état Helm sur le cluster pour les pods suspendus à la main.

    Un pod mis à 0 par `kubectl scale` garde `global.suspend=false` : Onyxia
    l'affiche « Démarrage du conteneur » à tort. On pose alors suspend=true (le
    nombre de réplicas reste 0, aucun pod n'est touché).
    """
    import subprocess
    from ._helm import find_helm, ensure_helm_repo, helm_env
    replicas = _statefulset_replicas(namespace)
    wanted = set(releases) if releases else None
    todo, skipped = [], []
    for r in _helm_releases(namespace):
        chart = r.get("chart", "")
        base = next((c for c in INTERACTIVE_CHARTS if chart.startswith(c + "-")), "")
        if not base or r.get("status") != "deployed":
            continue
        if wanted is not None and r["name"] not in wanted:
            continue
        sts = replicas.get(f"{r['name']}-{base}")
        if sts is None or sts > 0:
            continue                       # pas de StatefulSet, ou pod actif
        if _helm_suspended(r["name"], namespace):
            continue                       # déjà cohérent
        todo.append((r["name"], base, chart[len(base) + 1:]))
    if dry_run:
        return {"dry_run": True, "a_synchroniser": [t[0] for t in todo]}
    helm = find_helm()
    env = helm_env()
    ensure_helm_repo(helm, "https://inseefrlab.github.io/helm-charts-interactive-services")
    done, failed = [], []
    for name, base, version in todo:
        up = subprocess.run(
            [helm, "upgrade", name, f"inseefrlab/{base}", "-n", namespace,
             "--version", version, "--reuse-values", "--set", "global.suspend=true"],
            capture_output=True, text=True, timeout=90, env=env)
        (done if up.returncode == 0 else failed).append(name)
        if up.returncode != 0:
            skipped.append({name: up.stderr.strip()[-120:]})
    return {"synchronisees": done, "echecs": failed, "details_echecs": skipped}


def adopt(namespace: str, releases: list[str] | None = None,
          dry_run: bool = False) -> dict:
    """Rend visibles dans Onyxia des releases existantes (Secret catalogue
    `ide`, annotation d'origine `adopted`). Sans `releases` : toutes les candidates."""
    cands = candidates(namespace)
    wanted = set(releases) if releases else None
    todo = [c for c in cands if wanted is None or c["release"] in wanted]
    missing = sorted((wanted or set()) - {c["release"] for c in cands})
    if dry_run:
        return {"dry_run": True, "a_enregistrer": [c["release"] for c in todo],
                "ignorees": missing}
    done, failed = [], []
    for c in todo:
        ok = register(c["release"], namespace, c["release"], catalog=CATALOG,
                      origin=ORIGIN_ADOPTED)
        (done if ok else failed).append(c["release"])
    return {"enregistrees": done, "echecs": failed, "ignorees": missing}


def unregister(release: str, namespace: str) -> bool:
    rc, _, _ = T.kubectl("delete", "secret", secret_name(release),
                         "--ignore-not-found", namespace=namespace, timeout=20)
    return rc == 0
