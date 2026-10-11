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


def _helm_values(release: str, namespace: str) -> dict:
    """Valeurs effectives de la release (`helm get values -a`)."""
    import json
    import subprocess
    from ._helm import find_helm, helm_env
    r = subprocess.run([find_helm(), "get", "values", release, "-n", namespace,
                        "-a", "-o", "json"], capture_output=True, text=True,
                       timeout=30, env=helm_env())
    try:
        return json.loads(r.stdout or "{}")
    except Exception:
        return {}


def _helm_suspended(release: str, namespace: str) -> bool:
    return bool(_helm_values(release, namespace).get("global", {}).get("suspend"))


_UNITS = {"Ki": 2 ** 10, "Mi": 2 ** 20, "Gi": 2 ** 30, "Ti": 2 ** 40}


def _bytes(q: str) -> int:
    """Quantité Kubernetes (`20Gi`) en octets ; 0 si illisible."""
    m = re.fullmatch(r"(\d+)(Ki|Mi|Gi|Ti)?", (q or "").strip())
    return int(m.group(1)) * _UNITS.get(m.group(2) or "", 1) if m else 0


def helm_upgrade_values(release: str, namespace: str,
                        sets: dict[str, str]) -> tuple[bool, str]:
    """`helm upgrade --reuse-values --set k=v…` à version de chart constante.

    Retourne (ok, message d'erreur). Sert à garder Helm cohérent avec le cluster
    (suspend, taille du volume) pour que les boutons d'Onyxia restent utilisables.
    Sauf demande explicite (`global.suspend` dans `sets`), l'état de suspension du
    pod est conservé tel qu'il est sur le cluster.
    """
    import subprocess
    from ._helm import find_helm, ensure_helm_repo, helm_env
    try:
        rel = next((r for r in _helm_releases(namespace) if r["name"] == release), None)
        if rel is None:
            return False, "release introuvable"
        chart = rel.get("chart", "")
        base = next((c for c in INTERACTIVE_CHARTS if chart.startswith(c + "-")), "")
        if not base:
            return False, f"chart non géré : {chart}"
        helm = find_helm()
        ensure_helm_repo(
            helm, "https://inseefrlab.github.io/helm-charts-interactive-services")
        sets = dict(sets)
        if "global.suspend" not in sets:
            # Un upgrade ne doit jamais démarrer ni arrêter un pod. Sur une release
            # `failed`, --reuse-values reprend les valeurs de la dernière révision
            # *déployée* : on fige donc la suspension sur l'état réel du pod.
            n = _statefulset_replicas(namespace).get(f"{release}-{base}")
            if n is not None:
                sets["global.suspend"] = "true" if n == 0 else "false"
        cmd = [helm, "upgrade", release, f"inseefrlab/{base}", "-n", namespace,
               "--version", chart[len(base) + 1:], "--reuse-values"]
        for k, v in sets.items():
            cmd += ["--set", f"{k}={v}"]
        up = subprocess.run(cmd, capture_output=True, text=True, timeout=90,
                            env=helm_env())
        return up.returncode == 0, up.stderr.strip()[-300:]
    except Exception as e:
        return False, str(e)[:200]


def pvc_capacity(release: str, namespace: str) -> str:
    """Capacité réelle du PVC de la release ('' si introuvable)."""
    for base in INTERACTIVE_CHARTS:
        rc, out, _ = T.kubectl("get", "pvc", f"{release}-{base}", "-o",
                               "jsonpath={.status.capacity.storage}",
                               namespace=namespace, timeout=15)
        if rc == 0 and out.strip():
            return out.strip()
    return ""


def sync_suspension(namespace: str, releases: list[str] | None = None,
                    dry_run: bool = False) -> dict:
    """Aligne l'état Helm sur le cluster pour les pods modifiés hors Helm.

    Les releases `failed` (upgrade refusé à cause du volume) sont aussi traitées : un
    nouvel upgrade réussi les remet en `deployed`.

    Deux écarts, corrigés indépendamment (réplicas et pods ne sont jamais touchés) :
    - pod mis à 0 par `kubectl scale` : `global.suspend` reste false et Onyxia
      l'affiche « Démarrage du conteneur » → suspend=true ;
    - volume agrandi par `kubectl patch pvc` : `persistence.size` reste plus petit
      que la capacité et Helm refuse tout upgrade (Suspendre/Reprendre) →
      persistence.size = capacité réelle.
    """
    replicas = _statefulset_replicas(namespace)
    wanted = set(releases) if releases else None
    todo: list[tuple[str, dict]] = []
    for r in _helm_releases(namespace):
        chart = r.get("chart", "")
        base = next((c for c in INTERACTIVE_CHARTS if chart.startswith(c + "-")), "")
        if not base or r.get("status") not in ("deployed", "failed"):
            continue
        if wanted is not None and r["name"] not in wanted:
            continue
        values = _helm_values(r["name"], namespace)
        sets: dict[str, str] = {}
        sts = replicas.get(f"{r['name']}-{base}")
        if sts == 0 and not values.get("global", {}).get("suspend"):
            sets["global.suspend"] = "true"
        cap = pvc_capacity(r["name"], namespace)
        if cap and _bytes(cap) > _bytes(str(values.get("persistence", {}).get("size", ""))):
            sets["persistence.size"] = cap
        if sets:
            todo.append((r["name"], sets))
    if dry_run:
        return {"dry_run": True, "a_synchroniser": [t[0] for t in todo],
                "ecarts": {n: s for n, s in todo}}
    done, failed, details = [], [], []
    for name, sets in todo:
        ok, err = helm_upgrade_values(name, namespace, sets)
        (done if ok else failed).append(name)
        if not ok:
            details.append({name: err[-120:]})
    return {"synchronisees": done, "echecs": failed, "details_echecs": details}


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
