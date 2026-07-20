# Guide utilisateur — sspcloud-mcp

Ce guide couvre la mise en route complète, les workflows types (dev CPU, entraînement
GPU, déploiement de service) et le dépannage. Pour l'installation rapide et la référence
des outils, voir le [README](../README.md).

---

## 1. Modèle mental

`sspcloud-mcp` transforme un pod SSPCloud en **poste de travail piloté par un agent**.
Deux idées clés :

1. **Le kernel Jupyter est un REPL Python à état persistant.** Entre deux appels `exec`,
   les variables, imports et le **modèle GPU chargé restent en mémoire**. C'est ce qui
   permet une boucle d'évaluation rapide sans recharger le modèle à chaque étape.
2. **Le compute est hébergé côté admin.** Un utilisateur SSPCloud `stsonly` ne peut pas
   piloter de pods ; le serveur MCP s'exécute donc avec un **contexte kubectl admin** et
   agit comme *broker* : il lance, scale et pilote les pods pour l'agent.

---

## 2. Prérequis détaillés

### 2.1 kubectl + kubeconfig SSPCloud

Copier le snippet depuis `datalab.sspcloud.fr → Mon compte → Kubernetes`, puis vérifier :

```bash
kubectl config get-contexts
kubectl get pods -n user-VOTRE_NS      # doit répondre (pas "Forbidden")
```

> **Contrainte `stsonly`.** Si `kubectl get pods` renvoie *Forbidden* alors que vous êtes
> bien authentifié, votre compte a la policy `stsonly` : il ne dispose d'**aucun droit
> Kubernetes**. Le compute doit alors passer par un **compte disposant des droits**
> (namespace où vous pouvez `helm install` / `kubectl exec`). Renseignez ce contexte dans
> `PASSERELLE_KUBE_CONTEXT` pour que le serveur l'utilise quel que soit le contexte actif.

> **kubectl ≥ 1.26 et OIDC.** Les versions récentes ont retiré le plugin OIDC intégré ;
> un kubeconfig au format `auth-provider: oidc` peut expirer sans se rafraîchir. Recopiez
> un kubeconfig frais depuis l'UI Onyxia si vous obtenez *Unauthorized*.

### 2.2 helm

```bash
helm version
helm repo add inseefrlab https://inseefrlab.github.io/helm-charts-interactive-services
```

### 2.3 Pod substrat

Le serveur attache par défaut un pod de dev persistant (`PASSERELLE_MCP_POD`). Créez-le
une fois :

```bash
helm upgrade --install passerelle-mcp-dev inseefrlab/jupyter-python \
  -n user-VOTRE_NS \
  --set global.suspend=false \
  --set-string security.password=$(openssl rand -hex 16) \
  --set persistence.enabled=true --set persistence.size=10Gi
```

Le pod obtenu s'appelle `passerelle-mcp-dev-jupyter-python-0`. Le **`security.password`
défini ici EST le token Jupyter** — le serveur le récupère automatiquement.

---

## 3. Transports

| Transport | Quand | Comment |
|---|---|---|
| **port-forward** (défaut, validé) | Serveur MCP sur un PC avec kubectl admin | `kubectl port-forward` → kernel via `localhost`. Court-circuite l'ingress. |
| **URL publique + token** | Utilisateur sans kubectl | REST OK ; **WS kernel non fiable via l'ingress Onyxia** (limite connue). |
| **in-cluster** (V2) | Serveur hébergé dans un pod | Kernel joint via DNS interne `svc.cluster.local` — supprime la limite WS. |

Le token Jupyter est lu depuis le pod (`jupyter server list`) ou fourni ; il n'est jamais
stocké en clair hors de la session courante.

---

## 4. Workflows

### 4.1 Développement CPU (repo → tests)

```
session_start()                                  # attache passerelle-mcp-dev
push_repo(source="github.com/org/mon-projet")    # git clone
exec(lang="bash", code="pip install -e . -q")
exec(lang="bash", code="pytest -q")
read_file(path="…/rapport.txt")
```

### 4.2 Entraînement / inférence GPU (avec bascule de slot)

Un seul GPU par utilisateur : `gpu_switch` alloue le slot au projet, en préemptant les
autres pods GPU si besoin. L'environnement de chaque projet est préservé (PVC), donc les
bascules ultérieures sont rapides.

```
project_start(project="claris-open", gpu=true)   # 1ʳᵉ fois : helm install (~5-8 min)
push_repo(source="github.com/org/claris", session_id="gpu-claris-open")
exec(session_id="gpu-claris-open",
     code="import torch; print(torch.cuda.get_device_name(0))")
# entraînement long → écrire les checkpoints sur un volume persistant / S3
exec(lang="bash", code="nohup python train.py --out /home/onyxia/work/ckpt > train.log 2>&1 &")
# suivre l'avancement
exec(lang="bash", code="tail -n 30 train.log")
pull_artifact(path="ckpt/best.pt")
gpu_release(project="claris-open")               # libère le slot pour un autre projet
```

> **Pods éphémères.** SSPCloud suspend les instances inactives. Un entraînement long doit
> **écrire ses checkpoints** (volume `persistence` ou S3) et savoir **reprendre** — ne pas
> compter sur un notebook laissé ouvert.

### 4.3 Déploiement d'un service *(extra `[service]`)*

```
service_scaffold(name="mon-api", module="app.main:app", port=8000)
service_provision(yaml_path="mon-api.service.yml", packages="…", subdirs="core,api")
service_deploy(yaml_path="mon-api.service.yml")   # → URL publique HTTPS
service_status(yaml_path="mon-api.service.yml")
```

---

## 5. Dépannage

| Symptôme | Cause probable | Action |
|---|---|---|
| `KUBECTL_FORBIDDEN` | Contexte `stsonly` ou mauvais namespace | Renseigner `PASSERELLE_KUBE_CONTEXT` (compte admin). |
| `Unauthorized` sur kubectl | Token OIDC du kubeconfig expiré | Recopier un kubeconfig frais depuis l'UI Onyxia. |
| `POD_UNREACHABLE` | Pod suspendu (auto-suspend) | `session_start` relance le port-forward ; sinon `project_start`. |
| `session_start` sans effet | `PASSERELLE_MCP_POD` absent et aucun pod jupyter | Créer le pod substrat (§2.3) ou passer `attach_pod`. |
| `exec` Python renvoie vide | Kernel mort | `exec` recrée le kernel ; sinon `session_stop` + `session_start`. |
| `EXEC_TIMEOUT` | Traitement long en synchrone | Lancer en tâche de fond (`nohup … &`) et suivre via `tail`. |
| `NO_GPU_QUOTA` | Slot GPU occupé par un autre pod | `gpu_switch(preempt=true)` ou `gpu_release` sur l'autre projet. |
| Outils `service_*` en erreur d'import | Extra `[service]` non installé | `pip install -e .[service]`. |
| Cold start GPU très long | Normal (~5-8 min la 1ʳᵉ fois) | Attendre ; les bascules suivantes sont rapides. |

---

## 6. Sécurité

- L'authentification SSPCloud reste **locale** (kubeconfig, `~/.passerelle/`). Aucun secret
  n'est renvoyé à l'agent ni exposé dans les réponses d'outils.
- Le serveur hérite des droits kubectl configurés — son périmètre se limite au namespace
  ciblé (isolation SSPCloud native).
- `exec` exécute du code arbitraire dans un **pod jetable et isolé** : c'est voulu (bac à
  sable). Les commandes sont journalisées côté serveur.

---

## 7. Aller plus loin

- **Connecteur MCP distant OAuth** (mobile) : voir la feuille de route du README et les
  modules `server_http.py`, `oauth.py`, `start_mcp.py`.
- **Note d'architecture d'origine** : `docs/mcp-agent-compute-plan.md` dans le dépôt
  Passerelle (contraintes SSPCloud, décisions de transport, patterns de délégation).
