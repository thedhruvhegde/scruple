{{- define "scruple.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "scruple.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/* Metadata labels for every object (no component: selectors add their own). */}}
{{- define "scruple.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{ include "scruple.instanceLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "scruple.instanceLabels" -}}
app.kubernetes.io/name: {{ include "scruple.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/* API pods only: the Service, PDB and NetworkPolicy must never pick up Job or test pods. */}}
{{- define "scruple.selectorLabels" -}}
{{ include "scruple.instanceLabels" . }}
app.kubernetes.io/component: api
{{- end }}

{{- define "scruple.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "scruple.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "scruple.image" -}}
{{- if .Values.image.digest -}}
{{ .Values.image.repository }}@{{ .Values.image.digest }}
{{- else -}}
{{ .Values.image.repository }}:{{ default .Chart.AppVersion .Values.image.tag }}
{{- end -}}
{{- end }}

{{/* Guard rails that fail the render instead of deploying something unsafe. */}}
{{- define "scruple.validate" -}}
{{- if not (regexMatch "^postgres(ql)?://" .Values.database.url) }}
{{- fail "database.url must be a postgresql:// URL (SQLite cannot be shared by replicas)" }}
{{- end }}
{{- if regexMatch "^postgres(ql)?://[^/@]*:[^/@]*@" .Values.database.url }}
{{- fail "database.url must not contain a password; use database.passwordSecret" }}
{{- end }}
{{- if not .Values.database.passwordSecret.name }}
{{- fail "database.passwordSecret.name is required (an existing Secret)" }}
{{- end }}
{{- range $k, $v := .Values.config.extraEnv }}
{{- if regexMatch "(?i)(password|secret|token|salt|key)" $k }}
{{- fail (printf "config.extraEnv.%s looks like a secret; use a secretKeyRef, never a value" $k) }}
{{- end }}
{{- end }}
{{- end }}

{{/* Secret env vars: references only. Used by the Deployment and the migration Job. */}}
{{- define "scruple.secretEnv" -}}
- name: SCRUPLE_DATABASE_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.database.passwordSecret.name }}
      key: {{ .Values.database.passwordSecret.key }}
{{- if .Values.metrics.tokenSecret.name }}
- name: SCRUPLE_METRICS_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ .Values.metrics.tokenSecret.name }}
      key: {{ .Values.metrics.tokenSecret.key }}
{{- end }}
{{- if .Values.heldoutSalt.secret.name }}
- name: SCRUPLE_HELDOUT_SALT
  valueFrom:
    secretKeyRef:
      name: {{ .Values.heldoutSalt.secret.name }}
      key: {{ .Values.heldoutSalt.secret.key }}
{{- end }}
{{- end }}

{{/* Non-secret settings as key: value pairs (ConfigMap data; inline env for the Job). */}}
{{- define "scruple.configData" -}}
SCRUPLE_DATABASE_URL: {{ .Values.database.url | quote }}
SCRUPLE_LOG_LEVEL: {{ .Values.config.logLevel | quote }}
SCRUPLE_LOG_FORMAT: {{ .Values.config.logFormat | quote }}
SCRUPLE_API_HOST: "0.0.0.0"
SCRUPLE_API_PORT: "8000"
SCRUPLE_WEB_WORKERS: {{ .Values.config.webWorkers | quote }}
SCRUPLE_WEB_TIMEOUT: {{ .Values.config.webTimeout | quote }}
SCRUPLE_WEB_GRACEFUL_TIMEOUT: {{ .Values.config.webGracefulTimeout | quote }}
SCRUPLE_WEB_KEEPALIVE: {{ .Values.config.webKeepalive | quote }}
SCRUPLE_WEB_MAX_REQUESTS: {{ .Values.config.webMaxRequests | quote }}
SCRUPLE_READY_AUDIT_TTL_SECONDS: {{ .Values.config.readyAuditTtlSeconds | quote }}
SCRUPLE_METRICS_PUBLIC: {{ .Values.config.metricsPublic | quote }}
SCRUPLE_OTEL_ENABLED: {{ .Values.config.otelEnabled | quote }}
SCRUPLE_OTEL_SERVICE_NAME: {{ .Values.config.otelServiceName | quote }}
{{- if .Values.config.otelExporterEndpoint }}
OTEL_EXPORTER_OTLP_ENDPOINT: {{ .Values.config.otelExporterEndpoint | quote }}
{{- end }}
SCRUPLE_MIGRATE_ON_START: "false"
PROMETHEUS_MULTIPROC_DIR: /tmp/metrics
{{- range $k, $v := .Values.config.extraEnv }}
{{ $k }}: {{ $v | quote }}
{{- end }}
{{- end }}
