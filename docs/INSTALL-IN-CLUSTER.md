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

## Étape 4 — Vérifier le rôle du pod (`edit`)

L'installation crée un Deployment → le pod doit avoir le rôle Kubernetes **`edit`**
(choisi **au lancement du service** depuis le portail). Vérifiez dans le terminal :

```bash
kubectl auth can-i create deployment -n $(cat /var/run/secrets/kubernetes.io/serviceaccount/namespace)
```

Si la réponse est **`no`**, votre pod est en `view` : relancez un service Jupyter/VSCode
depuis le portail en réglant **Kubernetes → rôle = `edit`**. (Un pod lancé par le MCP a le
rôle `view` par défaut.)

---

## Étape 5 — Installer le service DURABLE (une commande)

**Une seule commande** déploie le serveur comme **Deployment Kubernetes** : il devient le
process principal d'un pod géré par K8s → **redémarrage automatique, jamais auto-suspendu,
survit à la fermeture de vos pods Jupyter**. Elle crée le Service + Ingress (URL propre),
génère la **clé API** stable, et affiche vos infos de connexion.

```bash
curl -fsSL https://raw.githubusercontent.com/nic01asFr/sspcloud-mcp/main/install.sh | bash
```

Le script déploie l'image **`ghcr.io/nic01asfr/sspcloud-mcp`** (chart Helm GitHub).
Le service apparaît dans **Mes services**.

Sortie (exemple) :

```
URL du connecteur : https://user-VOTRE_USER-mcp.user.lab.sspcloud.fr/mcp
Clé API (bearer)  : e0833472509c36472d0dd507b8a825f889bcacec…
```

**Où retrouver la clé API ?** Affichée par la commande, et récupérable à tout moment :
`kubectl get secret mcp -o jsonpath='{.data.PASSERELLE_MCP_BEARER}' | base64 -d ; echo`.
La page OAuth ne la délivre pas — elle la **vérifie**.

Puis, dans **Claude Desktop / mobile / claude.ai** → *Paramètres → Connecteurs → Ajouter un
connecteur MCP* → coller l'**URL**. Claude ouvre le formulaire d'autorisation ci-dessous :
collez-y la **clé API**. Les **24 outils** apparaissent.

![Formulaire d'autorisation OAuth « Autoriser Claude »](img/04-oauth-authorize.png)

> **Catalogue Onyxia :** une fois la source Helm ajoutée
> (`https://raw.githubusercontent.com/nic01asFr/sspcloud-mcp/main/helm-repo`),
> le lancement se fait en un clic. Voir [INSTALL-CATALOG.md](INSTALL-CATALOG.md).
> La commande ci-dessus fait déjà apparaître le service dans « Mes services ».
>
> Mettre à jour l'image : relancer la même commande, ou
> `kubectl rollout restart deployment/mcp -n <ns>` (la clé est conservée).
> Retirer : `helm uninstall mcp -n <ns>` ou depuis « Mes services ».

> **Dépannage rapide (non durable) :** `scripts/mcp_expose.sh` lance le serveur dans le
> terminal — pratique pour un test ponctuel, mais il **tombe en ~10 min** (fermeture des
> terminaux Jupyter / auto-suspension). Préférez toujours `install.sh` ci-dessus.

---

## Périmètre & sécurité

- **Mono-namespace** : chaque commande n'agit que dans votre namespace, avec vos droits.
- **Aucun accès inter-utilisateur**, **aucun compte central privilégié**.
- Vos identifiants (mot de passe Jupyter, jetons) restent **dans votre pod** ; rien n'est
  renvoyé à l'agent.
