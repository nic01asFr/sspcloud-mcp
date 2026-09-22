#!/bin/bash
# mcp_deploy.sh — Déploie le serveur MCP comme un Deployment Kubernetes DURABLE.
#
# Contrairement à mcp_expose.sh (qui lance le serveur dans le terminal d'un pod
# Jupyter — fragile : tué au cull du terminal ou à l'auto-suspension du pod), ce
# script crée un **Deployment** : le serveur est le process principal d'un pod
# géré par Kubernetes. Il redémarre tout seul en cas de crash (restartPolicy
# Always) et, n'étant PAS un service interactif Onyxia, il n'est jamais
# auto-suspendu. C'est le même principe que n8n / grist-coder.
#
# À exécuter UNE FOIS dans un terminal d'un pod jupyter-python (compte quelconque,
# même stsonly). Réutilise le ServiceAccount du pod (qui porte le rôle edit) —
# indispensable, car un compte stsonly ne peut pas créer de RoleBinding.
#
# Voie supportée (chart + image GHCR + Mes services) :
#   curl -fsSL https://raw.githubusercontent.com/nic01asFr/sspcloud-mcp/main/install.sh | bash
#
# Ce fichier reste un repli kubectl apply. Ne pas le documenter comme install.
#
# Idempotent : réutilise le Secret mcp-bearer et les noms mcp-http / mcp-ingress,
# donc l'URL et la clé API ne changent pas entre deux exécutions.

set -e
NAME="${MCP_NAME:-mcp}"                       # URL : user-<user>-<NAME>
IMAGE="${MCP_IMAGE:-ghcr.io/nic01asfr/sspcloud-mcp:latest}"
NS=$(cat /var/run/secrets/kubernetes.io/serviceaccount/namespace)
USER=${NS#user-}
HOST="${NS}-${NAME}.user.lab.sspcloud.fr"
WORK=/home/onyxia/work
SA=$(kubectl get pod "$HOSTNAME" -n "$NS" -o jsonpath='{.spec.serviceAccountName}')

echo "[mcp_deploy] namespace=$NS user=$USER host=$HOST"
echo "[mcp_deploy] serviceAccount réutilisé (rôle edit) : $SA"
[ -z "$SA" ] && { echo "[mcp_deploy] ERREUR : SA du pod introuvable"; exit 1; }

# 1. Clé API (bearer) : Secret K8s (clé 'bearer'). Réutilisé si présent, sinon
#    généré. Upsert idempotent pour garantir la clé (sinon secretKeyRef échoue).
CUR=$(kubectl get secret mcp-bearer -n "$NS" -o jsonpath='{.data.bearer}' 2>/dev/null | base64 -d 2>/dev/null || true)
if [ -z "$CUR" ]; then
  NEW=$(python -c "import secrets;print(secrets.token_hex(24))" 2>/dev/null || openssl rand -hex 24)
  kubectl create secret generic mcp-bearer -n "$NS" --from-literal=bearer="$NEW" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
fi
BEARER=$(kubectl get secret mcp-bearer -n "$NS" -o jsonpath='{.data.bearer}' | base64 -d)
mkdir -p "$WORK"
printf %s "$BEARER" > "$WORK/.mcp_bearer" 2>/dev/null || true

# 2. Deployment (serveur = process principal) + Service + Ingress.
#    Le bearer est injecté depuis le Secret (jamais en clair dans le manifeste).
kubectl apply -f - >/dev/null <<YAML
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${NAME}
  namespace: ${NS}
  labels: { app: ${NAME} }
spec:
  replicas: 1
  selector: { matchLabels: { app: ${NAME} } }
  strategy: { type: Recreate }
  template:
    metadata:
      labels: { app: ${NAME} }
    spec:
      serviceAccountName: ${SA}
      containers:
      - name: mcp
        image: ${IMAGE}
        imagePullPolicy: Always
        command: ["python","-m","sspcloud_mcp.server_http"]
        env:
        - { name: PORT, value: "8000" }
        - { name: SSPCLOUD_NAMESPACE, value: "${NS}" }
        - { name: ONYXIA_USER, value: "${USER}" }
        - { name: PASSERELLE_MCP_PUBLIC_URL, value: "https://${HOST}" }
        - name: PASSERELLE_MCP_BEARER
          valueFrom:
            secretKeyRef: { name: mcp-bearer, key: bearer }
        ports: [ { containerPort: 8000 } ]
        readinessProbe:
          httpGet: { path: /health, port: 8000 }
          initialDelaySeconds: 20
          periodSeconds: 10
        resources:
          requests: { cpu: "100m", memory: "512Mi" }
          limits:   { cpu: "1",    memory: "2Gi" }
---
apiVersion: v1
kind: Service
metadata: { name: ${NAME}-http, namespace: ${NS} }
spec:
  selector: { app: ${NAME} }
  ports: [ { name: mcp, port: 8000, targetPort: 8000 } ]
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: ${NAME}-ingress
  namespace: ${NS}
  annotations:
    nginx.ingress.kubernetes.io/proxy-read-timeout: "600"
    nginx.ingress.kubernetes.io/proxy-body-size: "0"
spec:
  ingressClassName: onyxia
  rules:
  - host: ${HOST}
    http:
      paths:
      - path: /
        pathType: Prefix
        backend: { service: { name: ${NAME}-http, port: { number: 8000 } } }
  tls:
  - hosts: [ "${HOST}" ]
YAML
echo "[mcp_deploy] Deployment + Service + Ingress appliqués"

# 3. Attendre le déploiement (pull image + pip install + démarrage serveur).
kubectl rollout status deployment/${NAME} -n "$NS" --timeout=180s || true

# 4. Fiche de connexion (persistée sur le PVC).
cat > "$WORK/MCP_CONNEXION.txt" <<EOF
========================================================
  VOTRE SERVICE MCP « Agent Compute » — SSPCloud (DURABLE)
========================================================

  URL du connecteur : https://${HOST}/mcp
  Clé API (bearer)  : ${BEARER}

  --- Connecter Claude (Desktop / mobile / claude.ai) ---
  Paramètres → Connecteurs → Ajouter un connecteur MCP
  Coller l'URL ci-dessus. Au formulaire OAuth, saisir la clé API.

  Le serveur tourne comme Deployment Kubernetes : redémarrage
  automatique, jamais auto-suspendu. Il survit à la fermeture de
  ce pod. Pour l'arrêter : kubectl delete deployment ${NAME} -n ${NS}

  Health : https://${HOST}/health
========================================================
EOF

echo
cat "$WORK/MCP_CONNEXION.txt"
echo "[mcp_deploy] health = $(curl -s -m8 https://${HOST}/health || echo 'démarrage en cours (vérifier dans 1 min)')"
