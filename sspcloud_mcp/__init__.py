"""
sspcloud_mcp — Serveur MCP « Agent Compute » pour SSPCloud / Onyxia.

Donne à un agent LLM (Claude Code, Claude Desktop, tout client MCP) un poste de
travail distant sur SSPCloud : pousser un repo (local ou GitHub) dans un pod,
écrire / exécuter / tester / évaluer du code, exploiter le GPU et des kernels
Jupyter stateful, rapatrier les livrables — le tout piloté par outils MCP.

Cœur autonome (aucune dépendance externe hors websockets). Les outils service_*
(déploiement de services SSPCloud) requièrent l'extra [service] (passerelle-sdk).

Voir README.md et docs/GUIDE.md.
"""

__version__ = "0.2.0"

from .kernel_client import KernelClient, ExecResult
from .session import SessionManager, DevSession
from .errors import MCPToolError

__all__ = ["KernelClient", "ExecResult", "SessionManager", "DevSession",
           "MCPToolError", "__version__"]
