# Changelog

## 0.3.0

Service **durable** + **hébergement natif Onyxia** — 24 outils.

### Durabilité (fin des services qui tombent)
- **`scripts/mcp_deploy.sh`** : déploie le serveur comme **Deployment Kubernetes**
  (process principal du pod) au lieu d'un lancement dans un terminal Jupyter.
  Redémarrage automatique, **jamais auto-suspendu**, survit à la fermeture des pods.
  Remplace `mcp_expose.sh` (relégué au dépannage rapide, fragile : fauché au cull
  des terminaux / à l'auto-suspension).
- Mesuré : un pod utilisateur **ne peut pas** créer de RoleBinding (même vers
  `edit`) → le script réutilise le ServiceAccount `edit` du pod courant.

### Chart de catalogue Onyxia (`charts/sspcloud-mcp/`)
- Self-install **en un clic** depuis le catalogue : Onyxia crée SA dédié +
  RoleBinding `edit`, Deployment, Service, Ingress, et le service apparaît dans
  **« Mes services »**. Bearer auto-généré stable (`resource-policy: keep`),
  affiché dans `NOTES.txt`. Publié au registre Helm GitLab par la CI.
- Voie CLI (`charts/sspcloud-mcp/scripts/install.sh`) pour un pod `edit`.

### OAuth navigateur (claude.ai / mobile)
- **CORS** (en-têtes `Access-Control-Allow-Origin` + préflight `OPTIONS`) et
  **`WWW-Authenticate`** (RFC 9728) : le flux OAuth exécuté dans le navigateur de
  claude.ai fonctionne (avant : « impossible de s'inscrire »).

### Nouveaux outils
- **`expose_public` / `unexpose_public`** : publier un port d'un pod en URL HTTPS
  (Service + Ingress via le SA). 22 → 24 outils.
- Fix regex du jeton Jupyter (accepte tout jeton, pas seulement hexadécimal).

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
