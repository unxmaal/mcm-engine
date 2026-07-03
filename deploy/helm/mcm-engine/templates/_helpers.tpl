{{/* Expand the name of the chart. */}}
{{- define "mcm-engine.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Fully qualified app name. */}}
{{- define "mcm-engine.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "mcm-engine.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "mcm-engine.labels" -}}
helm.sh/chart: {{ include "mcm-engine.chart" . }}
{{ include "mcm-engine.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "mcm-engine.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mcm-engine.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "mcm-engine.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "mcm-engine.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* Name of the bundled Postgres workload/service. */}}
{{- define "mcm-engine.postgres.fullname" -}}
{{- printf "%s-postgres" (include "mcm-engine.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Secret that holds the Postgres password + assembled DSN. */}}
{{- define "mcm-engine.dsnSecretName" -}}
{{- if and (not .Values.postgresql.enabled) .Values.externalDatabase.existingSecret -}}
{{- .Values.externalDatabase.existingSecret -}}
{{- else -}}
{{- include "mcm-engine.fullname" . -}}
{{- end -}}
{{- end -}}

{{/*
Fail fast on missing required config, then render nothing. Called from a template
that is always evaluated.
*/}}
{{- define "mcm-engine.validate" -}}
{{- if .Values.postgresql.enabled -}}
{{- if not .Values.postgresql.auth.password -}}
{{- fail "postgresql.enabled is true but no password set: set postgresql.auth.password (or use an external database)" -}}
{{- end -}}
{{- else -}}
{{- if and (not .Values.externalDatabase.dsn) (not .Values.externalDatabase.existingSecret) -}}
{{- fail "postgresql.enabled is false but no external database given: set externalDatabase.dsn or externalDatabase.existingSecret" -}}
{{- end -}}
{{- end -}}
{{- end -}}
