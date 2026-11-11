"""
sspcloud_mcp.errors — Codes d'erreur normalisés pour l'agent LLM.

Chaque erreur porte un `code` machine, un `message` humain et un `hint`
actionnable : l'agent doit pouvoir décider seul de la prochaine action.
"""

from __future__ import annotations


class MCPToolError(Exception):
    """Erreur d'outil MCP avec code + hint destinés à l'agent."""

    def __init__(self, code: str, message: str, hint: str = "",
                 extra: dict | None = None):
        self.code = code
        self.message = message
        self.hint = hint
        # Champs machine (ex. suggested_tools) : un petit modele suit mieux un
        # champ structure qu'une phrase.
        self.extra = dict(extra or {})
        super().__init__(f"[{code}] {message}")

    def to_dict(self) -> dict:
        return {"error": {"code": self.code,
                          "message": self.message,
                          "hint": self.hint,
                          **self.extra}}


# ── Codes standards ───────────────────────────────────────────────────────────

def no_session(session_id: str) -> MCPToolError:
    return MCPToolError(
        "NO_SESSION",
        f"Session inconnue : {session_id!r}.",
        "Appelez session_start pour en créer une, ou session_status pour lister.",
    )


def pod_unreachable(pod: str, detail: str = "") -> MCPToolError:
    return MCPToolError(
        "POD_UNREACHABLE",
        f"Pod {pod!r} injoignable. {detail}".strip(),
        "Le pod est peut-être suspendu (auto-suspend SSPCloud). "
        "Réessayez session_start pour le relancer.",
    )


def kubectl_forbidden(detail: str = "") -> MCPToolError:
    return MCPToolError(
        "KUBECTL_FORBIDDEN",
        f"Accès kubectl refusé. {detail}".strip(),
        "Le kubeconfig est probablement périmé ou pointe sur le mauvais "
        "namespace. Vérifiez le contexte et le token OIDC (passerelle login).",
    )


def kernel_error(detail: str) -> MCPToolError:
    return MCPToolError(
        "KERNEL_ERROR",
        f"Erreur du kernel Jupyter : {detail}",
        "Le kernel est peut-être mort. exec le recrée automatiquement ; "
        "si l'erreur persiste, relancez la session.",
    )


def exec_timeout(seconds: float) -> MCPToolError:
    return MCPToolError(
        "EXEC_TIMEOUT",
        f"Exécution dépassée ({seconds:.0f}s, plafond d'un appel synchrone).",
        "Pour un traitement long (entraînement...), utilisez background=true "
        "puis job_poll pour suivre l'avancement.",
    )


def session_busy(session_id: str, waited: float) -> MCPToolError:
    return MCPToolError(
        "SESSION_BUSY",
        f"Session {session_id!r} occupée par un exec précédent "
        f"(attente {waited:.0f}s).",
        "Un appel antérieur tourne encore sur ce kernel. Réessayez dans "
        "quelques secondes, ou lancez le traitement long avec background=true.",
    )


def kernel_start_timeout(session_id: str, seconds: float, pod: str = "") -> MCPToolError:
    cible = f"pod/{pod}" if pod else "pod/<nom du pod>"
    return MCPToolError(
        "KERNEL_START_TIMEOUT",
        f"Kernel de la session {session_id!r} non prêt en {seconds:.0f}s.",
        "Si le pod vient de démarrer ou de reprendre, attendez 30 à 60 s puis réessayez "
        "UNE fois. Après 2 échecs de suite, ne réessayez plus : appelez "
        f"workload_status(target='{cible}') pour savoir si le pod tourne, puis "
        "workload_logs pour comprendre pourquoi. Un pod qui n'exécute pas Jupyter "
        "répond NOT_A_JUPYTER_POD.",
        extra={"suggested_tools": ["workload_status", "workload_logs", "workload_events"],
               "target": cible},
    )


def not_a_jupyter_pod(pod: str, namespace: str = "") -> MCPToolError:
    cible = f"pod/{pod}"
    return MCPToolError(
        "NOT_A_JUPYTER_POD",
        f"Le pod {pod!r} n'exécute pas Jupyter : exec, read_file, write_file, "
        "list_files, job_poll et push_repo n'y fonctionnent pas.",
        "Pour le lire sans noyau : workload_status, workload_logs ou workload_events "
        f"(target='{cible}'). Pour exécuter du code, session_start sur un pod jupyter-* "
        "(list_pods indique les pods du namespace). Pour un shell seul (si SHELL_MODE_ENABLED "
        "est actif) : session_start(attach_pod=..., mode='shell').",
        extra={"suggested_tools": ["workload_status", "workload_logs", "workload_events"],
               "target": cible,
               **({"namespace": namespace} if namespace else {})},
    )


def no_kernel_in_shell_mode(session_id: str) -> MCPToolError:
    return MCPToolError(
        "NO_KERNEL_IN_SHELL_MODE",
        f"Session {session_id!r} en mode shell : exec_python n'est pas disponible.",
        "Utilisez exec(lang='bash', ...) ou exec(code='...', background=true).",
    )


def shell_mode_disabled() -> MCPToolError:
    return MCPToolError(
        "SHELL_MODE_DISABLED",
        "Le mode shell est désactivé sur ce serveur.",
        "Définissez SHELL_MODE_ENABLED=true dans l'environnement du pod MCP "
        "pour l'activer (opt-in explicite requis).",
    )


def unknown_argument(tool: str, proches: dict[str, str], inconnus: list[str]) -> MCPToolError:
    suggestions = ", ".join(f"{a!r} -> {b!r}" for a, b in proches.items())
    return MCPToolError(
        "UNKNOWN_ARGUMENT",
        f"{tool} : argument(s) inconnu(s) : {', '.join(sorted(inconnus))}.",
        f"Vouliez-vous dire : {suggestions} ? Rien n'a été exécuté.",
        extra={"suggestions": proches},
    )


def no_gpu_quota(detail: str = "") -> MCPToolError:
    return MCPToolError(
        "NO_GPU_QUOTA",
        f"Aucun slot GPU disponible. {detail}".strip(),
        "Un autre pod occupe le quota GPU. Réessayez session_gpu(preempt=true) "
        "ou libérez un pod avec session_stop.",
    )


def bad_source(source: str) -> MCPToolError:
    return MCPToolError(
        "BAD_SOURCE",
        f"Source de repo non reconnue : {source!r}.",
        "Fournissez une URL git (https://.../repo.git, github.com/org/repo) "
        "ou un chemin local existant.",
    )
