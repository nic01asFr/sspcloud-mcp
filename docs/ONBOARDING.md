# Installation individuelle — de zéro à opérationnel

> 📸 **Version illustrée (recommandée) :** pour l'installation **dans un pod SSPCloud**
> (fonctionne même en compte restreint `stsonly`), suivez **[INSTALL-IN-CLUSTER.md](INSTALL-IN-CLUSTER.md)**,
> avec captures d'écran étape par étape. Le présent guide couvre l'alternative « depuis un
> PC avec kubectl » (compte disposant des droits Kubernetes).

Ce guide s'adresse à **chaque utilisateur** qui veut configurer `sspcloud-mcp` pour
**son propre compte SSPCloud**. Usage individuel : vous pilotez **vos** pods dans
**votre** namespace, avec **votre** kubeconfig. Rien n'est partagé avec un autre compte.

Durée : ~15 min. Remplacez partout `VOTRE_USER` par votre identifiant SSPCloud
(celui de `user-VOTRE_USER`).

---

## Prérequis

- Un compte **SSPCloud** (datalab.sspcloud.fr) avec des **droits Kubernetes dans votre
  namespace** (rôle Onyxia `admin`/`edit` — le cas standard).
- **Python ≥ 3.9**, **git**, **kubectl** et **helm** installés.
- Un client MCP : **Claude Desktop** ou **Claude Code**.

---

## Étape 1 — Récupérer votre kubeconfig SSPCloud

1. Allez sur `https://datalab.sspcloud.fr` → **Mon compte → Kubernetes**.
2. Copiez le snippet `kubectl` et exécutez-le (il écrit votre `~/.kube/config`).
3. **Vérifiez vos droits** — c'est l'étape qui valide que tout le reste marchera :

```bash
kubectl config current-context
kubectl get pods -n user-VOTRE_USER      # doit répondre (liste vide OK), PAS "Forbidden"
```

> Si vous obtenez **`Forbidden`**, votre compte est restreint (`stsonly`) : voir la note
> en fin de guide. Sinon, continuez.

Notez le **nom de votre contexte** (`kubectl config current-context`) — il ira dans
`PASSERELLE_KUBE_CONTEXT`.

---

## Étape 2 — Installer sspcloud-mcp

```bash
git clone https://github.com/nic01asFr/sspcloud-mcp.git
cd sspcloud-mcp
pip install -e .
```

Vérifiez :

```bash
sspcloud-mcp --help 2>/dev/null; python -c "import sspcloud_mcp; print(sspcloud_mcp.__version__)"
```

---

## Étape 3 — Créer votre pod substrat (une fois)

C'est le pod de travail par défaut que le service attachera. Créez-le dans **votre**
namespace :

```bash
helm repo add inseefrlab https://inseefrlab.github.io/helm-charts-interactive-services
helm repo update

helm upgrade --install passerelle-mcp-dev inseefrlab/jupyter-python \
  -n user-VOTRE_USER \
  --set global.suspend=false \
  --set-string security.password=$(openssl rand -hex 16) \
  --set persistence.enabled=true --set persistence.size=10Gi
```

Le pod obtenu s'appelle `passerelle-mcp-dev-jupyter-python-0`. Le mot de passe généré
sert de token Jupyter — le service le récupère tout seul, vous n'avez rien à noter.

> Alternative sans pod permanent : sautez cette étape et utilisez `project_start` /
> `session_start(launch_chart="jupyter-python")` pour créer des pods à la demande.

---

## Étape 4 — Configurer votre client MCP

Trois variables, avec **vos** valeurs :

| Variable | Votre valeur |
|---|---|
| `SSPCLOUD_NAMESPACE` | `user-VOTRE_USER` |
| `PASSERELLE_KUBE_CONTEXT` | le contexte noté à l'étape 1 |
| `PASSERELLE_MCP_POD` | `passerelle-mcp-dev-jupyter-python-0` |

### Claude Desktop
`%APPDATA%\Roaming\Claude\claude_desktop_config.json` (Windows) — ajoutez dans `mcpServers` :

```json
"sspcloud-mcp": {
  "command": "sspcloud-mcp",
  "env": {
    "SSPCLOUD_NAMESPACE": "user-VOTRE_USER",
    "PASSERELLE_KUBE_CONTEXT": "VOTRE_CONTEXTE",
    "PASSERELLE_MCP_POD": "passerelle-mcp-dev-jupyter-python-0"
  }
}
```

Puis **quittez complètement** Claude Desktop (icône barre des tâches → Quitter) et relancez.

### Claude Code
`.mcp.json` à la racine de votre projet :

```json
{
  "mcpServers": {
    "sspcloud-mcp": {
      "command": "sspcloud-mcp",
      "env": {
        "SSPCLOUD_NAMESPACE": "user-VOTRE_USER",
        "PASSERELLE_KUBE_CONTEXT": "VOTRE_CONTEXTE",
        "PASSERELLE_MCP_POD": "passerelle-mcp-dev-jupyter-python-0"
      }
    }
  }
}
```

> `sspcloud-mcp` pas trouvé ? Utilisez `"command": "python", "args": ["-m",
> "sspcloud_mcp.server"]` et ajoutez `"PYTHONPATH": "chemin/vers/sspcloud_mcp"` dans `env`.

---

## Étape 5 — Vérifier

Dans votre client, demandez à l'agent de lister vos pods, puis :

```
session_start()                          # attache votre pod substrat
exec(code="print('hello', 6*7)")         # doit répondre 42
session_stop(session_id=...)
```

Si `session_start` renvoie votre pod et `exec` affiche `42`, c'est opérationnel.

---

## Étape 6 — Utiliser

Voir le [GUIDE](GUIDE.md) pour les workflows (dev CPU, entraînement GPU avec bascule de
slot, déploiement de service) et le [README](../README.md) pour la référence des 24 outils.

Boucle type :

```
session_start()
push_repo(source="github.com/org/mon-projet")
exec(lang="bash", code="pip install -e . -q && pytest -q")
gpu_switch(project="mon-projet")          # si besoin de GPU
exec(code="import torch; ...")
pull_artifact(path="results/out.gpkg")
session_stop(session_id=...)
```

---

## Périmètre & isolation

Le service est **strictement individuel et mono-namespace** :

- Il n'opère que dans **votre** namespace (`SSPCLOUD_NAMESPACE`), avec **votre**
  kubeconfig. Chaque commande kubectl est scopée à ce namespace.
- Il **n'accède jamais aux pods d'un autre utilisateur** et ne repose sur **aucun compte
  privilégié** central. Votre instance ne sert que vous.
- Vos identifiants (kubeconfig, token Jupyter) restent **locaux** : rien n'est renvoyé à
  l'agent ni partagé.

> Si l'étape 1 renvoie `Forbidden`, votre compte n'a pas de droits Kubernetes dans votre
> propre namespace (configuration inhabituelle). Contactez l'équipe : le chemin nominal
> suppose un compte avec droits kubectl sur `user-VOTRE_USER`, comme pour un poste standard.
