# Installation illustrée — sspcloud-mcp dans votre namespace SSPCloud

Ce guide montre, écran par écran, comment **installer le service pour vous-même**, dans
**votre** namespace SSPCloud, avec les droits de **votre** pod.

> **Usage strictement individuel.** Le service tourne dans votre namespace, opère avec le
> ServiceAccount de votre pod, et n'accède **jamais** aux pods d'un autre utilisateur.
> Il fonctionne **même si votre compte est restreint** (`stsonly`, sans droits kubectl
> directs) : c'est le pod, pas votre jeton personnel, qui porte les droits.

Durée : ~10 min.

---

## Étape 1 — Ouvrir « Mes services »

Connectez-vous sur [datalab.sspcloud.fr](https://datalab.sspcloud.fr), puis menu
**Mes services** (barre de gauche).

![Mes services](img/01-mes-services.png)

---

## Étape 2 — Lancer un service Jupyter-python

Cliquez **Nouveau service** → catalogue **Interactive services** → carte **Jupyter-python**
→ **Lancer**.

![Catalogue de services](img/02-catalogue.png)

> Les valeurs par défaut conviennent. Vous **n'avez pas besoin de noter le mot de passe** :
> le service le lit tout seul dans le pod.

---

## Étape 3 — Ouvrir un terminal

Depuis **Mes services**, cliquez **Ouvrir** sur votre Jupyter (un écran affiche vos
identifiants — vous n'en avez pas besoin, cliquez pour ouvrir). Dans JupyterLab, ouvrez un
**Terminal** depuis le *Launcher* (section **Other**) :

![Launcher JupyterLab — tuile Terminal](img/03-jupyterlab-launcher.png)

---

## Étape 4 — Installer le service (une commande)

Dans le terminal :

```bash
pip install "git+https://gitlab.cerema.fr/mcp/sspcloud_mcp.git"
```

> Si le dépôt est privé, ajoutez un token de lecture :
> `pip install "git+https://oauth2:<VOTRE_TOKEN>@gitlab.cerema.fr/mcp/sspcloud_mcp.git"`

Vérifier :

```bash
python -c "import sspcloud_mcp; print('installé', sspcloud_mcp.__version__)"
```

À partir de là, le service peut, **depuis votre pod**, lancer et piloter des workers
(dev/GPU) **dans votre namespace** — validé end-to-end (self-drive + lancement de worker).

---

## Étape 5 — Exposer le service en connecteur OAuth (self-service, 1 commande)

**Une seule commande** dans le terminal du pod fait tout : installe le serveur, crée le
**Service + Ingress** (URL publique propre) via le ServiceAccount du pod, génère la **clé
API**, démarre le serveur avec un **watchdog** (survit aux redémarrages), et écrit vos
infos de connexion dans `~/work/MCP_CONNEXION.txt`.

```bash
curl -sf https://gitlab.cerema.fr/mcp/sspcloud_mcp/-/raw/main/scripts/mcp_expose.sh | bash
```

Sortie (exemple) :

```
URL du connecteur : https://user-VOTRE_USER-mcp.user.lab.sspcloud.fr/mcp
Clé API (bearer)  : e0833472509c36472d0dd507b8a825f889bcacec…
```

Puis, dans **Claude Desktop / mobile / claude.ai** → *Paramètres → Connecteurs → Ajouter un
connecteur MCP* → coller l'**URL** ; au formulaire OAuth, saisir la **clé API**. Les 22 outils
apparaissent.

![Formulaire d'autorisation OAuth « Autoriser Claude »](img/04-oauth-authorize.png)

> **Validé end-to-end** sur un compte `stsonly` : le SA du pod peut créer Service, Ingress,
> Secret et piloter des pods. JupyterLab reste sur son URL (vous retrouvez votre terminal et
> `MCP_CONNEXION.txt` en rouvrant le pod) ; le MCP a **sa propre URL** dédiée.
>
> Retirer le service : `kubectl delete service mcp-http ingress mcp-ingress -n $(cat /var/run/secrets/kubernetes.io/serviceaccount/namespace)`

---

## Périmètre & sécurité

- **Mono-namespace** : chaque commande n'agit que dans votre namespace, avec vos droits.
- **Aucun accès inter-utilisateur**, **aucun compte central privilégié**.
- Vos identifiants (mot de passe Jupyter, jetons) restent **dans votre pod** ; rien n'est
  renvoyé à l'agent.
