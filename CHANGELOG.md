# Changelog

## 0.2.0

Serveur MCP « Agent Compute » pour SSPCloud / Onyxia — 22 outils.

### Cœur
- Sessions + kernel Jupyter **stateful** (`session_start/stop/status`, `exec`).
- Shell via le kernel, `push_repo` (git / local), fichiers, `pull_artifact`.
- Slot GPU arbitré (`gpu_switch/release/status`), projets, `list_pods`.
- Déploiement de services (`service_*`, extra `[service]`).
- **Mode background** : `exec background=true` + `job_poll` (traitements longs détachés).

### Fiabilité
- **Verrou d'exécution par session** — fin des `ConcurrencyError` (recv WS concurrent).
- **interrupt** du kernel sur timeout ; retry au démarrage (cold start).
- Correctifs in-cluster : token mode password (`$PASSWORD`), fast-path localhost,
  répertoires helm inscriptibles (`/tmp`), accès worker par IP pod, dérivation du
  username depuis le namespace (URL OAuth correcte).

### Hébergé / self-service (OAuth)
- Façade **Streamable HTTP + OAuth 2.1 + DCR (RFC 7591) + PKCE** (`server_http`, `oauth`).
- **`mcp_expose.sh`** : self-service en une commande — Service + Ingress via le
  ServiceAccount du pod, clé API (Secret K8s), watchdog de survie au reboot,
  `MCP_CONNEXION.txt`. Validé sur compte `stsonly`.
- **Permanence** via `init.personalInit` (Onyxia) + `mcp_watchdog.sh`.

### Qualité
- Tests unitaires (core + OAuth PKCE/DCR) + CI GitLab.
- Documentation : README (modes d'utilisation), GUIDE, ONBOARDING, INSTALL-IN-CLUSTER (illustré).
