# AGENTS.md — installer sspcloud-mcp avec un agent IA

Ce fichier s'adresse à un **assistant IA** (Claude, etc.) à qui on fournit ce dépôt
pour installer le service **avec** un utilisateur. Il donne la procédure, les
vérifications, et les pièges mesurés. Suivre les étapes dans l'ordre.

## 0. Ce qu'est le service

Un serveur **MCP** qui donne à un agent un poste de calcul distant sur SSPCloud
(exécuter du code, GPU, notebooks à état, déployer). Il tourne **dans le namespace
SSPCloud de l'utilisateur**, exposé en HTTPS, protégé par un bearer + OAuth.

## 1. Prérequis (à confirmer avec l'utilisateur)

- Un compte **SSPCloud** (datalab.sspcloud.fr).
- Un client MCP : **Claude Desktop / mobile / claude.ai** (connecteur OAuth) ou
  **Claude Code / Cursor** (header statique).

## 2. Choisir la voie d'installation

| Voie | Pour qui | Prérequis |
|---|---|---|
| **Catalogue Onyxia** (recommandé) | tout utilisateur, **un clic** | le chart doit être publié + le catalogue ajouté dans Onyxia (voir `docs/INSTALL-CATALOG.md`) |
| **CLI `install.sh`** (durable) | utilisateur technique / agent | un pod lancé avec `kubernetes.role: edit`. Script GitHub, image `ghcr.io/nic01asfr/sspcloud-mcp` |
| ~~`mcp_expose.sh`~~ | dépannage rapide seulement | **fragile** : tombe en ~10 min (cull terminal / auto-suspension). NE PAS recommander pour un usage durable. |

## 3. Piège RBAC à connaître (mesuré)

- Le rôle Kubernetes est **décidé au lancement du pod**. Un pod lancé par le MCP
  (`helm install` nu) ou par défaut a le rôle **`view` → aucun droit kubectl**.
- Seul un pod lancé **depuis le portail Onyxia avec `kubernetes.role: edit`** (ou
  `admin`) peut déployer.
- Un pod `edit` **ne peut pas** créer de RoleBinding (même vers `edit`) → l'install
  CLI **réutilise le SA du pod**. Seul le **catalogue** (install privilégié Onyxia)
  crée un SA dédié.

Donc : **demander à l'utilisateur de lancer un service Jupyter/VSCode avec le rôle
`edit`** avant l'install CLI. Vérifier : `kubectl auth can-i create deployment -n <ns>`.

## 4. Installer (voie CLI durable)

Dans un terminal d'un pod SSPCloud lancé en `kubernetes.role: edit` :

```bash
curl -fsSL https://raw.githubusercontent.com/nic01asFr/sspcloud-mcp/main/install.sh | bash
```

Le script tire le chart Helm depuis GitHub et l'image **`ghcr.io/nic01asfr/sspcloud-mcp`**
(pas un `pip install` depuis GitLab). Il crée un **Deployment durable**, génère le
bearer, enregistre le service dans **Mes services** (Secret `sh.onyxia.release.v1.mcp`),
et affiche l'URL + la clé. URL : `https://user-<idep>-mcp.user.lab.sspcloud.fr/mcp`.

## 5. Vérifier (l'agent DOIT faire ces contrôles)

```bash
B=https://user-<idep>-mcp.user.lab.sspcloud.fr
curl -s -o /dev/null -w '%{http_code}\n' $B/health                       # attendu 200
curl -s -o /dev/null -w '%{http_code}\n' -X OPTIONS $B/register \
  -H 'Origin: https://claude.ai' -H 'Access-Control-Request-Method: POST' # attendu 204 (CORS)
curl -s -o /dev/null -w '%{http_code}\n' -X POST $B/register \
  -H 'Content-Type: application/json' \
  -d '{"client_name":"c","redirect_uris":["https://claude.ai/api/mcp/auth_callback"]}'  # attendu 201 (DCR)
```

Et, avec le bearer, un `initialize` puis `tools/list` doivent renvoyer **24 outils**.
Si `OPTIONS` renvoie 501 → le pod tourne une image trop ancienne : `kubectl rollout
restart deployment/mcp -n <ns>` (l'image GHCR est en `pullPolicy: Always`).

## 6. Connecter Claude

- **OAuth** (Desktop / mobile / claude.ai) : Connecteurs → Ajouter → coller l'URL
  `…/mcp`. Au formulaire, saisir la clé (bearer).
- **Header statique** (Claude Code / Cursor `.mcp.json`) :
  ```json
  { "mcpServers": { "sspcloud-mcp": {
      "type": "http",
      "url": "https://user-<idep>-mcp.user.lab.sspcloud.fr/mcp",
      "headers": { "Authorization": "Bearer <clé>" } } } }
  ```

## 7. Maintenance

L'image est **`ghcr.io/nic01asfr/sspcloud-mcp`**. Pour tirer une mise à jour :
`kubectl rollout restart deployment/mcp -n <ns>` (ou relancer `install.sh`).
La clé bearer est conservée (`resource-policy: keep`), la config du client reste valable.

## 8. Références

- `README.md` — vue d'ensemble + référence des 24 outils.
- `docs/INSTALL-IN-CLUSTER.md` — install illustrée.
- `docs/GUIDE.md` — workflows (dev, GPU, notebooks, déploiement).
- `CHANGELOG.md` — historique.
