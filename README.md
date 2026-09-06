# sspcloud-mcp — Agent Compute pour SSPCloud / Onyxia

![MCP](https://img.shields.io/badge/MCP-server-3559FC)
![SSPCloud](https://img.shields.io/badge/SSPCloud-Onyxia-4775FF)
![OAuth](https://img.shields.io/badge/OAuth_2.1-DCR%20%2B%20PKCE-3559FC)
![Outils](https://img.shields.io/badge/outils-24-4775FF)
![Licence](https://img.shields.io/badge/licence-MIT-green)

**▶ [Présentation produit](https://nic01asfr.github.io/sspcloud-mcp/)** · [Installer](docs/INSTALL-IN-CLUSTER.md) · [Pour un agent IA](AGENTS.md) · [Changelog](CHANGELOG.md)

> Serveur **MCP** qui donne à un agent LLM (Claude Desktop, Claude Code, tout client
> MCP) un **poste de travail distant sur SSPCloud** : pousser un repo (local ou GitHub)
> dans un pod, écrire / exécuter / tester / évaluer du code, exploiter le **GPU** et des
> **kernels Jupyter à état persistant**, déployer des services, rapatrier les livrables —
> le tout piloté par des outils MCP.

---

## Pourquoi

SSPCloud (Onyxia, INSEE) offre du calcul GPU gratuit, mais un agent LLM ne peut pas
s'en servir seul :

- Un utilisateur SSPCloud standard a la policy **`stsonly`** → **aucun droit `kubectl`**
  (même en lecture dans son propre namespace) et l'API Onyxia `PUT /my-lab/app` est
  bloquée. Un agent ne peut donc **ni lancer ni piloter** un pod pour ce compte.
- Les pods Onyxia sont **éphémères, sans port entrant, sans IP publique**.

`sspcloud-mcp` lève ces verrous : il s'exécute avec un **contexte admin** (kubectl/helm
dans un namespace disposant des droits), lance/scale les pods, et pilote leur **kernel
Jupyter** — exposant tout cela à l'agent sous forme d'outils simples et bornés.

C'est précisément ce qui manque quand un agent dit *« il faut lancer un service GPU
depuis l'interface Onyxia, je ne peux pas le faire à ta place »*.

---

## Modes d'utilisation — quel mode pour qui

| Mode | Pour qui | Comment | Mobile |
|---|---|---|---|
| **Hébergé durable** (recommandé) | **tout collègue** | catalogue Onyxia en un clic, ou `install.sh` depuis un pod `edit` → connecteur OAuth ([INSTALL-IN-CLUSTER](docs/INSTALL-IN-CLUSTER.md)) | **oui** |
| **Local (stdio)** | admin / power-user avec droits kubectl | `.mcp.json` → `sspcloud-mcp` sur le PC ([ONBOARDING](docs/ONBOARDING.md)) | non |

- L'**hébergé durable** est le mode « produit » : chacun lance **son** service dans **son**
  namespace, comme **Deployment** (redémarrage auto, jamais auto-suspendu), exposé en
  HTTPS/OAuth, utilisable depuis Claude Desktop **et l'app mobile**. Le serveur agit avec un
  ServiceAccount `edit` ; aucun droit kubectl côté PC requis.
- Le **local** est le plus rapide si vous avez déjà kubectl configuré (aucun pod à déployer).

Même code (`sspcloud_mcp`) dans les deux cas — seule la façade change (stdio vs HTTP+OAuth).
Agent IA qui installe avec vous : voir **[AGENTS.md](AGENTS.md)**.

---

## En un coup d'œil

```
Agent LLM (Claude Desktop / Code)
   │  outils MCP
   ▼
sspcloud-mcp  ──►  kubectl / helm (contexte admin)   → lance/scale les pods
   │           └►  kernel Jupyter (WebSocket)         → exec Python STATEFUL
   ▼
Pod(s) SSPCloud  ── git clone · pip · pytest · GPU · notebooks · checkpoints S3
```

- **Exécution Python à état persistant** : le modèle chargé, les variables, les imports
  survivent entre les appels d'outils (kernel Jupyter maintenu vivant).
- **Shell via le kernel** : `git`, `pip`, `pytest`… tournent dans le pod sans dépendance
  supplémentaire.
- **Slot GPU arbitré** : 1 GPU/utilisateur SSPCloud → bascule par projet avec préemption,
  environnement de chaque projet préservé (PVC).

---

## Installation

> **Nouveau venu ?** Deux guides d'installation individuelle (votre compte, votre namespace) :
> - **[docs/INSTALL-IN-CLUSTER.md](docs/INSTALL-IN-CLUSTER.md)** — illustré, service dans un
>   pod SSPCloud (fonctionne même en compte restreint `stsonly`). **Recommandé.**
> - **[docs/ONBOARDING.md](docs/ONBOARDING.md)** — depuis un PC avec kubectl (compte avec droits).
>
> Usage **strictement individuel** : chaque personne installe le service pour son propre
> namespace, avec son propre kubeconfig. Aucun compte central, aucun accès aux pods
> d'autrui.

```bash
git clone https://gitlab.cerema.fr/mcp/sspcloud_mcp.git
cd sspcloud_mcp
pip install -e .            # cœur autonome (websockets)
# pip install -e .[service] # + outils de déploiement service_* (SDK Passerelle)
```

**Prérequis runtime** (côté machine qui héberge le serveur MCP) :

- `kubectl` et `helm` dans le `PATH`, avec un **kubeconfig configuré** pour SSPCloud
  (copié depuis `datalab.sspcloud.fr → Mon compte → Kubernetes`).
- Un **contexte disposant des droits** dans un namespace (compte admin). Voir
  [docs/GUIDE.md](docs/GUIDE.md) pour la contrainte `stsonly`.
- Python ≥ 3.9.

---

## Configuration client

### Claude Desktop

`%APPDATA%\Roaming\Claude\claude_desktop_config.json` (Windows) — ajouter dans `mcpServers` :

```json
"sspcloud-mcp": {
  "command": "sspcloud-mcp",
  "env": {
    "SSPCLOUD_NAMESPACE": "user-VOTRE_NS",
    "PASSERELLE_KUBE_CONTEXT": "VOTRE_CONTEXTE_ADMIN",
    "PASSERELLE_MCP_POD": "passerelle-mcp-dev-jupyter-python-0"
  }
}
```

> Si `sspcloud-mcp` n'est pas installé (`pip install`), remplacez par
> `"command": "python", "args": ["-m", "sspcloud_mcp.server"]` avec
> `"PYTHONPATH": "chemin/vers/sspcloud_mcp"` dans `env`.

Redémarrer Claude Desktop **complètement** (quitter depuis la zone de notification).

### Claude Code (`.mcp.json` de projet)

```json
{
  "mcpServers": {
    "sspcloud-mcp": {
      "command": "sspcloud-mcp",
      "env": {
        "SSPCLOUD_NAMESPACE": "user-VOTRE_NS",
        "PASSERELLE_KUBE_CONTEXT": "VOTRE_CONTEXTE_ADMIN",
        "PASSERELLE_MCP_POD": "passerelle-mcp-dev-jupyter-python-0"
      }
    }
  }
}
```

**Variables d'environnement**

| Variable | Rôle |
|---|---|
| `SSPCLOUD_NAMESPACE` | Namespace K8s cible (ex. `user-moncompte`). |
| `PASSERELLE_KUBE_CONTEXT` | **Épingle** le contexte kubectl (évite d'hériter d'un contexte `stsonly` sans droits). |
| `PASSERELLE_MCP_POD` | Pod substrat par défaut quand `session_start` est appelé sans argument. |

---

## Prise en main (workflow type)

```
session_start()                              # attache le pod substrat (env PASSERELLE_MCP_POD)
push_repo(source="github.com/org/repo")      # git clone dans le pod (ou chemin local)
exec(code="import torch; model = load()")    # Python — reste en mémoire
exec(lang="bash", code="pytest -q")          # shell dans le pod
gpu_switch(project="mon-projet")             # bascule le slot GPU vers un pod GPU
exec(code="preds = model(batch)")            # inférence / éval sur GPU
pull_artifact(path="results/out.gpkg")       # rapatrie le livrable en local
session_stop(session_id=...)                 # libère kernel + port-forward
```

---

## Référence des outils (24)

**Sessions & exécution**
| Outil | Rôle |
|---|---|
| `session_start` | Ouvre une session (attache un pod, URL publique, ou lance un pod). |
| `session_status` | État d'une session / liste des sessions. |
| `exec` | Exécute du code : `python` (kernel **stateful**), `bash` (shell), ou `background=true` (détaché). |
| `job_poll` | Suit un traitement lancé en `background` (running/terminé + log). |
| `session_stop` | Ferme la session (option `uninstall` = supprime le pod). |

**Code & fichiers**
| Outil | Rôle |
|---|---|
| `push_repo` | Repo → pod : URL git (clone) ou chemin local (upload). |
| `write_file` / `read_file` / `list_files` | Édition et lecture bornées dans le pod. |
| `pull_artifact` | Rapatrie un fichier du pod vers le PC (livrable, checkpoint). |

**GPU (slot unique arbitré)**
| Outil | Rôle |
|---|---|
| `gpu_status` | État du slot GPU (quel projet le détient) ou `nvidia-smi` du pod. |
| `gpu_switch` | Bascule le slot GPU vers le pod GPU d'un projet (préemption, PVC préservé). |
| `gpu_release` | Libère le slot GPU (scale 0) sans détruire l'environnement. |

**Projets & pods**
| Outil | Rôle |
|---|---|
| `project_start` | Lance un pod dédié pour un projet (CPU ou GPU). |
| `project_bind` | Attache une session à un pod existant (nom ou filtre projet). |
| `list_pods` | Liste les pods / statefulsets du namespace. |

**Exposition HTTPS publique**
| Outil | Rôle |
|---|---|
| `expose_public` | Publie un port du pod (app / PWA / démo) à une URL HTTPS — crée Service + Ingress `onyxia` via le ServiceAccount du pod. Retourne l'URL. |
| `unexpose_public` | Retire l'exposition (supprime Service + Ingress). |

**Déploiement de services** *(extra `[service]`)*
| Outil | Rôle |
|---|---|
| `service_scaffold` | Génère un `<name>.service.yml` minimal. |
| `service_provision` | Upload packages/modèles vers S3 (prérequis au deploy). |
| `service_deploy` | Déploie un service SSPCloud → URL publique HTTPS. |
| `service_status` / `service_stop` / `service_warm` | Cycle de vie du service. |

Guide détaillé, exemples et dépannage : **[docs/GUIDE.md](docs/GUIDE.md)**.

---

## Architecture

- **Transport** : `port-forward` vers le kernel Jupyter du pod (court-circuite l'ingress
  K8s) ; le token Jupyter = le `security.password` du pod. En pod (in-cluster), le kernel
  est joint via le DNS interne.
- **Cœur autonome** : `websockets` uniquement. `jupyter_protocol` (protocole ZMQ binaire)
  et les helpers helm sont vendorés.
- **Outils `service_*`** : reposent sur `passerelle-sdk` (extra `[service]`).

Détails de conception : voir la note d'architecture d'origine dans le dépôt Passerelle
(`docs/mcp-agent-compute-plan.md`).

---

## Limites connues

- Le transport **URL publique + token** (sans kubectl) fonctionne en REST mais le
  **WebSocket kernel ne traverse pas l'ingress Onyxia** de façon fiable → utiliser le
  `port-forward` (contexte admin). Résolu nativement par l'hébergement in-cluster (V2).
- **Cold start GPU** ~5-8 min à la première allocation d'un pod GPU.
- **1 seul GPU** par utilisateur SSPCloud → `gpu_switch` arbitre le slot (préemption).

---

## Hébergement durable + connecteur OAuth — livré ✅

Le serveur s'héberge **dans le namespace SSPCloud de l'utilisateur** comme **Deployment
Kubernetes** (process principal du pod) : **redémarrage automatique, jamais auto-suspendu,
survit à la fermeture des pods Jupyter**. Exposé en **connecteur MCP OAuth** (Streamable
HTTP + OAuth 2.1 + DCR RFC 7591 + PKCE, **CORS** pour le flux navigateur de claude.ai),
déclarable dans Claude Desktop **et l'app mobile**.

Deux voies d'installation :
- **Catalogue Onyxia** (recommandé, **un clic**) — chart `charts/sspcloud-mcp/` : Onyxia
  crée SA dédié + RoleBinding `edit`, et le service apparaît dans **« Mes services »**.
- **CLI** — `charts/sspcloud-mcp/scripts/install.sh` (ou `scripts/mcp_deploy.sh`) depuis un
  pod lancé en `kubernetes.role: edit`. Voir **[docs/INSTALL-IN-CLUSTER.md](docs/INSTALL-IN-CLUSTER.md)**
  et **[AGENTS.md](AGENTS.md)** (install pilotée par un agent IA).

> `scripts/mcp_expose.sh` (lancement dans le terminal) reste pour un **test rapide**, mais
> il est **fragile** (tombe en ~10 min) — préférez la voie durable ci-dessus.

---

## Licence

MIT — voir [LICENSE](LICENSE).
