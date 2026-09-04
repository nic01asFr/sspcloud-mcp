{{/* Helpers du chart sspcloud-mcp */}}

{{- define "sspcloud-mcp.fullname" -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "sspcloud-mcp.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "sspcloud-mcp.labels" -}}
helm.sh/chart: {{ include "sspcloud-mcp.chart" . }}
{{ include "sspcloud-mcp.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: onyxia
{{- end -}}

{{- define "sspcloud-mcp.selectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
  PASSERELLE_MCP_BEARER — garde Bearer du pod, stable entre upgrades :
    1. Si valeur explicite fournie (appAuth.token) → on l'utilise.
    2. Sinon, si Secret existe déjà → on réutilise (jamais régénéré).
    3. Sinon → on génère aléatoire 48 char.
  L'auteur du chart n'a jamais connaissance de ce token : il naît dans le
  namespace du user et n'en sort pas.
*/}}
{{- define "sspcloud-mcp.bearer" -}}
{{- if .Values.appAuth.token -}}
{{- .Values.appAuth.token -}}
{{- else -}}
{{- $existing := (lookup "v1" "Secret" .Release.Namespace (include "sspcloud-mcp.fullname" .)) -}}
{{- if and $existing $existing.data (index $existing.data "PASSERELLE_MCP_BEARER") -}}
{{- index $existing.data "PASSERELLE_MCP_BEARER" | b64dec -}}
{{- else -}}
{{- randAlphaNum 48 -}}
{{- end -}}
{{- end -}}
{{- end -}}
