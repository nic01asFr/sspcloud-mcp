#!/bin/bash
# mcp_expose.sh — Self-service : expose SON serveur MCP depuis SON pod SSPCloud.
#
# À exécuter UNE FOIS dans un terminal d'un pod jupyter-python (compte quelconque,
# même stsonly — utilise le ServiceAccount du pod, rôle edit). Installe le serveur,
# crée Service + Ingress (URL propre), génère la clé API, lance le serveur, et écrit
# les infos de connexion dans ~/work/MCP_CONNEXION.txt (persistant sur le PVC).
#
# Fragile (~10 min). Préférer :
#   curl -fsSL https://raw.githubusercontent.com/nic01asFr/sspcloud-mcp/main/install.sh | bash

set -e
REPO="https://github.com/nic01asFr/sspcloud-mcp"
RAW="https://raw.githubusercontent.com/nic01asFr/sspcloud-mcp/main"
NAME="${MCP_NAME:-mcp}"                       # nom du service (URL: user-<user>-<NAME>)
NS=$(cat /var/run/secrets/kubernetes.io/serviceaccount/namespace)
USER=${NS#user-}
POD=$HOSTNAME
HOST="${NS}-${NAME}.user.lab.sspcloud.fr"
WORK=/home/onyxia/work

echo "[mcp_expose] namespace=$NS pod=$POD host=$HOST"

# 1. Installer sspcloud_mcp (repo public)
python -c "import sspcloud_mcp" 2>/dev/null \
  || PYTHONPATH=$WORK/psdk python -c "import sspcloud_mcp" 2>/dev/null \
  || pip install --quiet "git+${REPO}.git"

# 2. Clé API (bearer) : Secret K8s (réutilisé si présent), sinon génération
BEARER=$(kubectl get secret mcp-bearer -n "$NS" -o jsonpath='{.data.bearer}' 2>/dev/null | base64 -d 2>/dev/null || true)
if [ -z "$BEARER" ]; then
  BEARER=$(python -c "import secrets;print(secrets.token_hex(24))")
  kubectl create secret generic mcp-bearer -n "$NS" --from-literal=bearer="$BEARER" >/dev/null 2>&1 || true
fi
printf %s "$BEARER" > "$WORK/.mcp_bearer"; chmod 600 "$WORK/.mcp_bearer"
printf %s "https://${HOST}" > "$WORK/.mcp_public_url"

# 3. Service K8s (port 8000 -> CE pod) + Ingress (URL propre -> service:8000)
kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Service
metadata:
  name: ${NAME}-http
  namespace: ${NS}
spec:
  selector:
    statefulset.kubernetes.io/pod-name: ${POD}
  ports:
  - name: mcp
    port: 8000
    targetPort: 8000
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
        backend:
          service:
            name: ${NAME}-http
            port:
              number: 8000
  tls:
  - hosts:
    - ${HOST}
EOF
echo "[mcp_expose] Service + Ingress créés"

# 4. Watchdog + démarrage du serveur (survit aux reboots via le hook Jupyter)
curl -sf "${RAW}/scripts/mcp_watchdog.sh" -o "$WORK/mcp_watchdog.sh"
mkdir -p /home/onyxia/.jupyter
curl -sf "${RAW}/scripts/jupyter_server_config.py" -o /home/onyxia/.jupyter/jupyter_server_config.py 2>/dev/null || true
setsid bash "$WORK/mcp_watchdog.sh" </dev/null >>"$WORK/mcp_boot.log" 2>&1 &

# 5. Attendre que le serveur réponde
for i in $(seq 1 20); do curl -sf http://localhost:8000/health >/dev/null 2>&1 && break; sleep 2; done

# 6. Fiche de connexion (persistée sur le PVC)
cat > "$WORK/MCP_CONNEXION.txt" <<EOF
========================================================
  VOTRE SERVICE MCP « Agent Compute » — SSPCloud
========================================================

  URL du connecteur : https://${HOST}/mcp
  Clé API (bearer)  : ${BEARER}

  --- Connecter Claude (Desktop / mobile / claude.ai) ---
  Paramètres → Connecteurs → Ajouter un connecteur MCP
  Coller l'URL ci-dessus. Au formulaire OAuth, saisir la clé API.

  Health : https://${HOST}/health
  (Ce fichier reste dans ~/work ; rouvrez le pod pour le retrouver.)
========================================================
EOF

echo
cat "$WORK/MCP_CONNEXION.txt"
echo "[mcp_expose] health = $(curl -s -m5 http://localhost:8000/health || echo 'en cours de démarrage')"
