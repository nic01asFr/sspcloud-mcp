"""
sspcloud_mcp.errors — Codes d'erreur normalisés pour l'agent LLM.

Chaque erreur porte un `code` machine, un `message` humain et un `hint`
actionnable : l'agent doit pouvoir décider seul de la prochaine action.
"""

from __future__ import annotations


class MCPToolError(Exception):
    """Erreur d'outil MCP avec code + hint destinés à l'agent."""

    def __init__(self, code: str, message: str, hint: str = ""):
        self.code = code
        self.message = message
        self.hint = hint
        super().__init__(f"[{code}] {message}")

    def to_dict(self) -> dict:
        return {"error": {"code": self.code,
                          "message": self.message,
                          "hint": self.hint}}


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
        f"Exécution dépassée ({seconds:.0f}s).",
        "Pour un traitement long (entraînement...), utilisez background=true "
        "puis job_poll pour suivre l'avancement.",
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
