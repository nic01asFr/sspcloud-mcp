"""
sspcloud_mcp.ops — Exploitation du namespace en LECTURE SEULE : état, journaux, événements.

Le service tourne avec le rôle `edit` du namespace. Ces outils lui permettent de dire à
l'agent ce qui se passe, de façon BORNÉE : pas de shell, pas de kubectl libre.

  - cibles : Deployment, StatefulSet ou Pod du namespace, jamais autre chose ;
  - lecture seule ; jetons, mots de passe et clés sont masqués dans les journaux ;
  - sorties bornées (lignes, caractères).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

from . import transports as T
from .errors import MCPToolError

_KINDS = {"deployment": "deployment", "deploy": "deployment",
          "statefulset": "statefulset", "sts": "statefulset",
          "pod": "pod", "po": "pod"}
_ORDRE_RECHERCHE = ("deployment", "statefulset", "pod")
_NOM = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
_SINCE = re.compile(r"^\d{1,4}[smhd]$")
MAX_LIGNES = 500
MAX_CARACTERES = 20_000

_SECRETS = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1***"),
    (re.compile(r"(?i)((?:token|password|passwd|secret|api[_-]?key|authorization)"
                r"\"?\s*[=:]\s*\"?)[^\s\",}]+"), r"\1***"),
    (re.compile(r"(?i)([?&](?:token|key|password)=)[^&\s]+"), r"\1***"),
    (re.compile(r"glpat-[A-Za-z0-9._-]+"), "glpat-***"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "gh*_***"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "<jwt>"),
]


def masquer(texte: str) -> str:
    """Masque jetons, mots de passe et clés d'un texte de journal."""
    for motif, remplacement in _SECRETS:
        texte = motif.sub(remplacement, texte)
    return texte


def _kubectl(*args: str, ns: str, timeout: int = 30) -> tuple[int, str, str]:
    rc, out, err = T.kubectl(*args, namespace=ns, timeout=timeout)
    T._raise_if_forbidden(rc, err)
    return rc, out, err


def _existe(kind: str, name: str, ns: str) -> bool:
    return _kubectl("get", kind, name, "-o", "name", ns=ns, timeout=15)[0] == 0


def resoudre(cible: str, ns: str) -> tuple[str, str]:
    """`deployment/mcp`, `statefulset/atelier`, `pod/atelier-0` ou un nom seul."""
    cible = (cible or "").strip().lower()
    if not cible:
        raise MCPToolError("TARGET_REQUIRED", "cible requise.",
                           "Ex : cible='mcp', 'statefulset/atelier' ou 'pod/atelier-0'.")
    if "/" in cible:
        k, name = cible.split("/", 1)
        kind = _KINDS.get(k)
        if not kind:
            raise MCPToolError("BAD_TARGET", f"Type non géré : {k!r}.",
                               "Types admis : deployment, statefulset, pod.")
        kinds = (kind,)
    else:
        name, kinds = cible, _ORDRE_RECHERCHE
    if not _NOM.match(name):
        raise MCPToolError("BAD_TARGET", f"Nom invalide : {name!r}.",
                           "Un nom Kubernetes : minuscules, chiffres, tirets.")
    for kind in kinds:
        if _existe(kind, name, ns):
            return kind, name
    raise MCPToolError("TARGET_NOT_FOUND", f"Introuvable dans {ns} : {cible}.",
                       "list_pods donne les pods et StatefulSets du namespace.")


def _json(kind: str, name: str, ns: str) -> dict:
    rc, out, err = _kubectl("get", kind, name, "-o", "json", ns=ns)
    if rc != 0:
        raise MCPToolError("TARGET_NOT_FOUND", f"{kind}/{name} : {err.strip()[:150]}")
    return json.loads(out)


def _pods_de(kind: str, obj: dict, ns: str) -> list[dict]:
    if kind == "pod":
        return [obj]
    labels = (obj.get("spec", {}).get("selector") or {}).get("matchLabels") or {}
    if not labels:
        return []
    sel = ",".join(f"{k}={v}" for k, v in labels.items())
    rc, out, _ = _kubectl("get", "pods", "-l", sel, "-o", "json", ns=ns)
    return json.loads(out).get("items", []) if rc == 0 and out.strip() else []


def _age_s(horodatage: str | None) -> int | None:
    if not horodatage:
        return None
    try:
        t = datetime.fromisoformat(horodatage.replace("Z", "+00:00"))
        return int((datetime.now(timezone.utc) - t).total_seconds())
    except ValueError:
        return None


def _etat_conteneur(cs: dict) -> str:
    state = cs.get("state") or {}
    if "running" in state:
        return "running"
    if "waiting" in state:
        w = state["waiting"]
        return f"waiting:{w.get('reason', '?')}"
    if "terminated" in state:
        t = state["terminated"]
        return f"terminated:{t.get('reason', '?')} (code {t.get('exitCode')})"
    return "inconnu"


def _resume_pod(p: dict) -> dict:
    st = p.get("status", {})
    css = st.get("containerStatuses") or []
    r = {
        "nom": p["metadata"]["name"],
        "phase": st.get("phase"),
        "prets": f"{sum(1 for c in css if c.get('ready'))}/{len(p['spec'].get('containers', []))}",
        "redemarrages": sum(c.get("restartCount", 0) for c in css),
        "age_s": _age_s(p["metadata"].get("creationTimestamp")),
        "ip": st.get("podIP"),
        "conteneurs": [{"nom": c["name"], "etat": _etat_conteneur(c),
                        "redemarrages": c.get("restartCount", 0)} for c in css],
    }
    if p["metadata"].get("deletionTimestamp"):
        r["en_arret"] = True
    if st.get("phase") == "Pending":
        # Cause d'un pod en attente (GPU indisponible, volume, image…).
        raisons = [f"{c.get('reason')}: {c.get('message', '')[:160]}"
                   for c in st.get("conditions", []) if c.get("status") == "False"]
        if raisons:
            r["attente"] = raisons
    return r


def statut(cible: str, ns: str) -> dict:
    kind, name = resoudre(cible, ns)
    obj = _json(kind, name, ns)
    res: dict = {"cible": f"{kind}/{name}", "namespace": ns}
    if kind != "pod":
        spec, st = obj.get("spec", {}), obj.get("status", {})
        res["replicas"] = {"voulus": spec.get("replicas"),
                           "prets": st.get("readyReplicas", 0),
                           "a_jour": st.get("updatedReplicas", 0)}
        res["images"] = [c["image"] for c in
                         spec.get("template", {}).get("spec", {}).get("containers", [])]
    else:
        res["images"] = [c["image"] for c in obj["spec"].get("containers", [])]
    res["pods"] = [_resume_pod(p) for p in _pods_de(kind, obj, ns)[:10]]
    return res


def journaux(cible: str, ns: str, lignes: int = 100, depuis: str = "",
             precedent: bool = False, conteneur: str = "") -> dict:
    kind, name = resoudre(cible, ns)
    lignes = max(1, min(int(lignes or 100), MAX_LIGNES))
    if depuis and not _SINCE.match(depuis):
        raise MCPToolError("BAD_ARGUMENT", f"depuis invalide : {depuis!r}.",
                           "Format : 30s, 10m, 2h ou 1d.")
    if conteneur and not _NOM.match(conteneur):
        raise MCPToolError("BAD_ARGUMENT", f"conteneur invalide : {conteneur!r}.")
    pods = [p["metadata"]["name"] for p in _pods_de(kind, _json(kind, name, ns), ns)]
    if not pods:
        return {"cible": f"{kind}/{name}", "pods": [],
                "note": "aucun pod en cours (suspendu ou arrêté) : voir workload_events."}
    sorties = []
    for pod in sorted(pods)[:3]:
        cmd = ["logs", pod, f"--tail={lignes}"]
        if depuis:
            cmd.append(f"--since={depuis}")
        if precedent:
            cmd.append("--previous")
        if conteneur:
            cmd += ["-c", conteneur]
        rc, out, err = _kubectl(*cmd, ns=ns, timeout=40)
        texte = masquer(out if rc == 0 else f"(pas de journal : {err.strip()[:200]})")
        sorties.append({"pod": pod, "tronque": len(texte) > MAX_CARACTERES,
                        "journal": texte[-MAX_CARACTERES:]})
    return {"cible": f"{kind}/{name}", "pods": sorties}


def evenements(cible: str, ns: str, limite: int = 30) -> dict:
    limite = max(1, min(int(limite or 30), 100))
    nom = ""
    if cible:
        _, nom = resoudre(cible, ns)
    rc, out, _ = _kubectl("get", "events", "-o", "json", ns=ns)
    items = json.loads(out).get("items", []) if rc == 0 and out.strip() else []
    ev = []
    for e in items:
        obj = e.get("involvedObject", {}).get("name", "")
        if nom and not (obj == nom or obj.startswith(nom + "-")):
            continue
        if not nom and e.get("type") != "Warning":
            continue                    # sans cible : seulement les alertes du namespace
        quand = (e.get("lastTimestamp") or e.get("eventTime")
                 or e["metadata"].get("creationTimestamp"))
        ev.append({"quand": quand, "type": e.get("type"), "raison": e.get("reason"),
                   "objet": f"{e.get('involvedObject', {}).get('kind', '')}/{obj}",
                   "message": masquer(e.get("message", ""))[:240],
                   "nombre": e.get("count", 1)})
    ev.sort(key=lambda x: x["quand"] or "")
    return {"cible": cible or "(alertes du namespace)", "evenements": ev[-limite:]}


def redemarrer(cible: str, ns: str, confirmer: bool = False) -> dict:
    """Redémarre un Deployment ou un StatefulSet (rollout restart), ou un pod à contrôleur.

    Sans confirmer=True : aperçu seulement, rien n'est exécuté. Un pod sans contrôleur
    n'est jamais supprimé (il ne serait pas recréé). Les cibles listées dans
    OPS_PROTEGES (variable d'environnement du service) sont refusées.
    """
    kind, name = resoudre(cible, ns)
    obj = _json(kind, name, ns)
    protegees = {x.strip() for x in os.getenv("OPS_PROTEGES", "").split(",") if x.strip()}
    if name in protegees or f"{kind}/{name}" in protegees:
        raise MCPToolError("TARGET_PROTECTED", f"{kind}/{name} est protégé.",
                           "Retirez-le de OPS_PROTEGES (configuration du service).")
    if kind == "pod":
        proprietaires = obj["metadata"].get("ownerReferences") or []
        if not proprietaires:
            raise MCPToolError(
                "BARE_POD", f"pod/{name} n'a pas de contrôleur : le supprimer le détruirait.",
                "Seuls les pods d'un StatefulSet, Deployment ou ReplicaSet se redémarrent.")
        action = (f"supprime pod/{name}, recréé par "
                  f"{proprietaires[0].get('kind')}/{proprietaires[0].get('name')}")
        concernes = [name]
    else:
        if kind == "statefulset" and not obj.get("spec", {}).get("replicas"):
            raise MCPToolError("NOTHING_TO_RESTART", f"{kind}/{name} est à 0 réplica (suspendu).",
                               "Reprenez-le d'abord (Onyxia, pod_register, gpu_switch).")
        action = f"rollout restart {kind}/{name}"
        concernes = [p["metadata"]["name"] for p in _pods_de(kind, obj, ns)]
    apercu = {"cible": f"{kind}/{name}", "action": action, "pods_concernes": concernes,
              "interruption": "brève : indisponible le temps du redémarrage"}
    if not confirmer:
        return {**apercu, "fait": False,
                "suite": "Rappelez workload_restart avec confirm=true pour l'exécuter."}
    if kind == "pod":
        rc, out, err = _kubectl("delete", "pod", name, "--wait=false", ns=ns, timeout=30)
    else:
        rc, out, err = _kubectl("rollout", "restart", f"{kind}/{name}", ns=ns, timeout=30)
    if rc != 0:
        raise MCPToolError("RESTART_FAILED", f"{action} : {err.strip()[:200]}")
    return {**apercu, "fait": True, "retour": out.strip()[:200],
            "suite": "workload_status pour suivre le retour à l'état prêt."}
