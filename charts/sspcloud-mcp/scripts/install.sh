#!/usr/bin/env bash
# install.sh — Déploie sspcloud-mcp (chart Helm) dans VOTRE namespace SSPCloud.
#
# Voie CLI (depuis un terminal d'un service SSPCloud lancé avec kubernetes.role: edit) :
#   curl -sL https://gitlab.cerema.fr/mcp/sspcloud_mcp/-/raw/main/charts/sspcloud-mcp/scripts/install.sh | bash
#
# Un pod utilisateur ne pouvant pas créer de RoleBinding (mesuré Forbidden, même
# vers la ClusterRole 'edit'), cette voie réutilise le ServiceAccount du pod
# courant (rôle edit). Pour un SA dédié + binding, passer par le CATALOGUE Onyxia
# (Onyxia crée le RBAC lui-même).
#
# Variables optionnelles : NAME (défaut mcp → URL user-<idep>-<NAME>), REF (branche/tag).
set -euo pipefail
REPO="https://gitlab.cerema.fr/mcp/sspcloud_mcp"
NAME="${NAME:-mcp}"
REF="${REF:-main}"

command -v kubectl >/dev/null || { echo "[KO] kubectl introuvable (lancer dans un pod SSPCloud)"; exit 1; }
command -v helm    >/dev/null || { echo "[KO] helm introuvable (lancer dans un pod SSPCloud)"; exit 1; }

NS=$(cat /var/run/secrets/kubernetes.io/serviceaccount/namespace)
IDEP="${NS#user-}"
SA=$(kubectl get pod "$HOSTNAME" -n "$NS" -o jsonpath='{.spec.serviceAccountName}')
HOST="user-${IDEP}-${NAME}.user.lab.sspcloud.fr"
echo "[--] namespace=$NS  SA(edit)=$SA  host=$HOST"

kubectl auth can-i create deployment -n "$NS" >/dev/null 2>&1 \
  || { echo "[KO] Ce pod n'a pas le rôle 'edit' (create deployment refusé). Relancez un service avec kubernetes.role: edit."; exit 1; }

# Répertoires Helm inscriptibles (sur les pods SSPCloud, ~/.config ne l'est pas).
_HB="${TMPDIR:-/tmp}/sspmcp-helm"
export HELM_CACHE_HOME="$_HB/cache" HELM_CONFIG_HOME="$_HB/config" HELM_DATA_HOME="$_HB/data"
mkdir -p "$HELM_CACHE_HOME" "$HELM_CONFIG_HOME" "$HELM_DATA_HOME"

# Récupère le chart (dépôt public).
TMP=$(mktemp -d)
git clone --depth 1 --branch "$REF" "${REPO}.git" "$TMP/src" >/dev/null 2>&1
CHART="$TMP/src/charts/sspcloud-mcp"

echo "[--] helm upgrade --install $NAME ..."
helm upgrade --install "$NAME" "$CHART" \
  --namespace "$NS" \
  --set "fullnameOverride=$NAME" \
  --set "app.host=$HOST" \
  --set "app.ref=$REF" \
  --set "onyxia_user=$IDEP" \
  --set "rbac.create=false" \
  --set "serviceAccount.name=$SA" \
  --wait --timeout 5m

BEARER=$(kubectl get secret "$NAME" -n "$NS" -o jsonpath='{.data.PASSERELLE_MCP_BEARER}' 2>/dev/null | base64 -d || echo "")
cat <<EOF

Déploiement terminé (Deployment durable, jamais auto-suspendu).

  URL du connecteur : https://$HOST/mcp
  Clé API (bearer)  : $BEARER
  Health            : https://$HOST/health

Coller l'URL dans Claude (Connecteurs) ; au formulaire OAuth, saisir la clé.
Arrêter : helm uninstall $NAME -n $NS   (la clé est conservée : resource-policy keep)
EOF
rm -rf "$TMP"
