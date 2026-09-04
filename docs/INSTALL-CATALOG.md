# Install en un clic — catalogue Onyxia

Le but : qu'un collègue lance **Catalogue → sspcloud-mcp → Lancer** et obtienne un
service **durable, visible dans « Mes services »**, sans aucune commande. Ceci décrit
la mise en place (une fois) puis l'usage (pour chacun).

## A. Publier le chart (une fois, automatique)

La CI (`.gitlab-ci.yml`, job `publish-chart`) **package et publie** le chart dans le
registre Helm intégré du projet GitLab à chaque push sur `main` touchant
`charts/sspcloud-mcp/`. L'URL du repo Helm pour les consommateurs est :

```
https://gitlab.cerema.fr/api/v4/projects/<PROJECT_ID>/packages/helm/stable
```

`<PROJECT_ID>` = l'ID numérique du projet `mcp/sspcloud_mcp` (page du projet GitLab →
sous le nom, ou *Settings → General*). Le job CI l'affiche aussi dans ses logs.

> Republier une nouvelle version : **bumper `version:`** dans
> `charts/sspcloud-mcp/Chart.yaml` (le registre versionne par numéro de chart).

## B. Ajouter le catalogue dans Onyxia (une fois)

Deux options :
1. **Catalogue personnel** — dans Onyxia, *Mon compte → Sources de catalogues* (selon la
   version), ajouter l'URL du repo Helm ci-dessus. Le service apparaît dans votre catalogue.
2. **Catalogue SSPCloud partagé** — demander à l'équipe SSPCloud d'ajouter la source pour
   que tous les agents CEREMA le voient. (Demande à formuler ; pas de droit d'admin requis
   côté utilisateur.)

## C. Lancer (pour chaque utilisateur)

1. **Catalogue → sspcloud-mcp → Lancer.** Laisser les valeurs par défaut (le rôle `edit`
   et l'URL sont réglés par le chart / Onyxia).
2. Onyxia crée le Deployment + SA dédié + RoleBinding `edit` + Ingress, et le service
   apparaît dans **« Mes services »**.
3. Cliquer **Ouvrir** : la fenêtre « Accès au service » affiche l'**URL du connecteur** et
   comment récupérer la **clé API** (`NOTES.txt`).
4. Connecter Claude (OAuth ou header statique) — voir la fiche affichée ou [AGENTS.md](../AGENTS.md) §6.

## Pourquoi c'est le bon modèle

- **Durable** : Deployment géré par K8s → redémarrage auto, jamais auto-suspendu.
- **Natif Onyxia** : visible et gérable dans « Mes services » (Secret de métadonnées créé
  par l'install catalogue).
- **Sécurisé** : bearer auto-généré stable (jamais connu de l'auteur du chart), OAuth
  DCR/PKCE pour les clients distants. Modèle validé (cf. axe interne « service Onyxia sécurisé »).
