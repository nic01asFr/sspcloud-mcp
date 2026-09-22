#!/usr/bin/env bash
# install.sh — Déploie sspcloud-mcp dans VOTRE namespace SSPCloud.
#
# Chart + script : GitHub (nic01asFr/sspcloud-mcp).
# Image          : ghcr.io/nic01asfr/sspcloud-mcp (tirée par le pod, pas de pip).
#
# Depuis un terminal d'un service Onyxia (Jupyter, VS Code) lancé avec
#   Kubernetes > accès depuis le service : oui
#   Kubernetes > rôle                   : edit
#
#   curl -fsSL https://raw.githubusercontent.com/nic01asFr/sspcloud-mcp/main/install.sh | bash
#
# Un pod utilisateur ne peut pas créer de RoleBinding : cette voie réutilise
# le ServiceAccount du pod courant. Le catalogue Onyxia, lui, crée le SA dédié.
set -euo pipefail

RELEASE="${RELEASE:-mcp}"
REPO_NAME="sspcloud-mcp"
HELM_REPO_RAW="https://raw.githubusercontent.com/nic01asFr/sspcloud-mcp/main/helm-repo"
GIT_REPO="https://github.com/nic01asFr/sspcloud-mcp.git"
IMAGE_REPO_DEFAULT="ghcr.io/nic01asfr/sspcloud-mcp"
REF="${REF:-main}"

SA_NS_FILE="/var/run/secrets/kubernetes.io/serviceaccount/namespace"
NS="${NAMESPACE:-${KUBERNETES_NAMESPACE:-}}"
if [[ -z "$NS" && -f "$SA_NS_FILE" ]]; then
  NS=$(cat "$SA_NS_FILE")
fi
USERNAME="${ONYXIA_USER:-${NS#user-}}"
if [[ -z "$NS" || -z "$USERNAME" ]]; then
  echo "ERREUR : impossible de détecter le namespace SSPCloud."
  echo "Lance ce script depuis un terminal d'un service Onyxia."
  exit 1
fi

K8S_DOMAIN="${K8S_DOMAIN:-${ONYXIA_DOMAIN:-user.lab.sspcloud.fr}}"
HOST="user-${USERNAME}-${RELEASE}.${K8S_DOMAIN}"

if [[ -t 1 ]]; then
  GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
else
  GREEN=""; YELLOW=""; RED=""; CYAN=""; RESET=""
fi
log()  { echo "${CYAN}[--]${RESET} $*"; }
ok()   { echo "${GREEN}[OK]${RESET} $*"; }
warn() { echo "${YELLOW}[!!]${RESET} $*"; }
die()  { echo "${RED}[KO]${RESET} $*" >&2; exit 1; }

log "Vérification des prérequis..."
command -v kubectl >/dev/null || die "kubectl introuvable."
command -v helm    >/dev/null || die "helm introuvable."

SA=$(kubectl get pod "$HOSTNAME" -n "$NS" -o jsonpath='{.spec.serviceAccountName}' 2>/dev/null || true)
if ! kubectl auth can-i create deployments -n "$NS" >/dev/null 2>&1; then
  die "Droits Kubernetes insuffisants dans $NS.

  Relance le pod terminal avec :
    Kubernetes > Enable access from within the service : oui
    Kubernetes > Kubernetes role                       : edit

  (Le rôle par défaut 'view' ne permet pas de créer le Deployment.)"
fi

SECRET_NAME="${RELEASE}"
BEARER=$(kubectl get secret "$SECRET_NAME" -n "$NS" \
  -o jsonpath='{.data.PASSERELLE_MCP_BEARER}' 2>/dev/null | base64 -d 2>/dev/null || echo "")
if [[ -z "$BEARER" ]]; then
  BEARER=$(head -c 48 /dev/urandom | base64 | tr -d '/+=' | cut -c1-48)
  log "Clé API : nouvelle valeur générée"
else
  ok "Clé API : valeur existante conservée"
fi

export HELM_CONFIG_HOME="${HELM_CONFIG_HOME:-/tmp/sspmcp-helm/config}"
export HELM_CACHE_HOME="${HELM_CACHE_HOME:-/tmp/sspmcp-helm/cache}"
export HELM_DATA_HOME="${HELM_DATA_HOME:-/tmp/sspmcp-helm/data}"
mkdir -p "$HELM_CONFIG_HOME" "$HELM_CACHE_HOME" "$HELM_DATA_HOME"

echo ""
echo "+==============================================================+"
echo "|  Installation SSPCloud MCP — $USERNAME"
echo "|  Namespace : $NS"
echo "|  URL       : https://$HOST/mcp"
echo "|  Image     : $IMAGE_REPO_DEFAULT"
echo "+==============================================================+"
echo ""

CHART_REF=""
log "[1/4] Dépôt Helm : $HELM_REPO_RAW"
if helm repo add "$REPO_NAME" "$HELM_REPO_RAW" --force-update >/dev/null 2>&1 \
   && helm repo update "$REPO_NAME" >/dev/null 2>&1; then
  CHART_REF="$REPO_NAME/sspcloud-mcp"
  ok "Helm repo OK"
else
  warn "Index Helm indisponible — repli : chart cloné depuis GitHub."
  command -v git >/dev/null || die "git introuvable, et le dépôt Helm n'est pas publié."
  TMP=$(mktemp -d)
  git clone --depth 1 --branch "$REF" "$GIT_REPO" "$TMP/src" >/dev/null
  CHART_REF="$TMP/src/charts/sspcloud-mcp"
  ok "Chart local : $CHART_REF"
fi

log "[2/4] Déploiement Helm (image GHCR)..."
ARGS=(
  --namespace "$NS"
  --set "fullnameOverride=$RELEASE"
  --set "app.host=$HOST"
  --set "onyxia_user=$USERNAME"
  --set "ingress.className=onyxia"
  --set "rbac.create=false"
  --set-string "appAuth.token=$BEARER"
  --set "image.repository=${IMAGE_REPO:-$IMAGE_REPO_DEFAULT}"
  --set "image.tag=${IMAGE_TAG:-latest}"
  --set "image.pullPolicy=Always"
)
[[ -n "$SA" ]] && ARGS+=(--set "serviceAccount.name=$SA")
[[ -n "${VERSION:-}" ]] && ARGS+=(--version "$VERSION")

helm upgrade --install "$RELEASE" "$CHART_REF" "${ARGS[@]}" --wait --timeout 8m
ok "Release $RELEASE à jour"

log "[3/4] Enregistrement Mes services (Onyxia)..."
ONYXIA_SECRET="sh.onyxia.release.v1.${RELEASE}"
if kubectl create secret generic "$ONYXIA_SECRET" -n "$NS" --type=onyxia.sh/release.v1 \
    --from-literal=owner="$USERNAME" \
    --from-literal=friendlyName="SSPCloud MCP" \
    --from-literal=catalog="sspcloud-mcp" \
    --from-literal=share="false" \
    --dry-run=client -o yaml 2>/dev/null | kubectl apply -f - >/dev/null 2>&1; then
  ok "Visible dans datalab > Mes services"
else
  warn "Secret Onyxia non enregistré (le pod reste accessible par son URL)"
fi

log "[4/4] Notes d'installation"
echo ""
helm get notes "$RELEASE" -n "$NS" 2>/dev/null | sed '1d' || true
echo ""
echo "+==============================================================+"
echo "|  Accès"
echo "+==============================================================+"
echo "|  Connecteur : https://$HOST/mcp"
echo "|  Clé API    : $BEARER"
echo "|  Santé      : https://$HOST/health"
echo "|"
echo "|  Mes services > SSPCloud MCP > Ouvrir (mêmes blocs)"
echo "+==============================================================+"
echo ""
echo "Désinstaller : helm uninstall $RELEASE -n $NS"
echo "               kubectl delete secret $ONYXIA_SECRET -n $NS"
