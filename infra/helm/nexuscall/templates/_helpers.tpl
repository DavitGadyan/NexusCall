{{/*
NexusCall shared template set. Every workload (16 services, web, workers) is normalised into one
"component" record by `nexuscall.components`; the resource templates range over that list, so a new
service or worker in scripts/gen_compose.py only needs `infra/helm/generate.py` to be re-run.
*/}}

{{- define "nexuscall.chart" -}}
{{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end -}}

{{/* Labels. Call with (dict "root" $ "c" $component "track" "stable") */}}
{{- define "nexuscall.labels" -}}
helm.sh/chart: {{ include "nexuscall.chart" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
app.kubernetes.io/part-of: nexuscall
app.kubernetes.io/version: {{ .root.Values.global.image.tag | default .root.Chart.AppVersion | quote }}
{{ include "nexuscall.selectorLabels" . }}
nexuscall.io/kind: {{ .c.kind }}
{{- end -}}

{{- define "nexuscall.selectorLabels" -}}
app.kubernetes.io/name: nexuscall
app.kubernetes.io/component: {{ .c.name }}
nexuscall.io/track: {{ .track | default "stable" }}
{{- end -}}

{{/* Image reference. Call with (dict "root" $ "image" "crm-service" "tag" "" "digest" "") */}}
{{- define "nexuscall.image" -}}
{{- $g := .root.Values.global.image -}}
{{- $repo := printf "%s/%s" (trimSuffix "/" $g.registry) .image -}}
{{- $digest := .digest | default $g.digest -}}
{{- if $digest -}}
{{ printf "%s@%s" $repo $digest }}
{{- else -}}
{{ printf "%s:%s" $repo (.tag | default $g.tag) }}
{{- end -}}
{{- end -}}

{{- define "nexuscall.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: {{ .Values.podSecurity.runAsUser }}
runAsGroup: {{ .Values.podSecurity.runAsGroup }}
fsGroup: {{ .Values.podSecurity.fsGroup }}
fsGroupChangePolicy: OnRootMismatch
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "nexuscall.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
runAsNonRoot: true
privileged: false
capabilities:
  drop: ["ALL"]
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{/* Vault KV v2 data path for a path relative to secret/<env>/ */}}
{{- define "nexuscall.kvPath" -}}
{{ printf "%s/data/%s/%s" .root.Values.global.vault.kvMount .root.Values.global.env .path }}
{{- end -}}

{{/*
Vault Agent template for the in-memory env file /vault/secrets/env, sourced by the container command.
Values are single-quoted for the shell; Vault-generated credentials are alphanumeric (password policy in
infra/terraform/vault-config) and operator-supplied secrets must not contain single quotes.
Call with (dict "root" $ "c" $component)
*/}}
{{- define "nexuscall.vaultEnvTemplate" -}}
{{- $root := .root -}}
{{- $c := .c -}}
{{- $g := $root.Values.global -}}
{{- if or (eq $c.kind "service") (eq $c.kind "worker") -}}
{{- $db := $c.database -}}
{{- range $suffix, $var := dict "app" "DATABASE_URL" "inspector" "INSPECTOR_DATABASE_URL" "platform" "PLATFORM_DATABASE_URL" }}
{{ printf "{{- with secret \"%s/creds/%s-%s\" }}" $g.database.vaultMount $db $suffix }}
{{ printf "export %s='postgresql+asyncpg://{{ .Data.username }}:{{ .Data.password }}@%s:%v/%s?ssl=%s'" $var $g.database.host $g.database.port $db $g.database.ssl }}
{{ "{{- end }}" }}
{{- end }}
{{ printf "{{- with secret \"%s/creds/%s\" }}" $g.rabbitmq.vaultMount $c.identity }}
{{ printf "export AMQP_URL='%s://{{ .Data.username }}:{{ .Data.password }}@%s:%v/%s'" $g.rabbitmq.scheme $g.rabbitmq.host $g.rabbitmq.port $g.rabbitmq.vhost }}
{{ "{{- end }}" }}
{{ printf "{{- with secret \"%s\" }}" (include "nexuscall.kvPath" (dict "root" $root "path" (printf "platform/%s/keycloak-client" $c.identity))) }}
{{ "export SERVICE_CLIENT_SECRET='{{ .Data.data.client_secret }}'" }}
{{ "{{- end }}" }}
{{- if has "objectstore" $c.infra }}
{{ printf "{{- with secret \"%s\" }}" (include "nexuscall.kvPath" (dict "root" $root "path" (printf "platform/%s/s3" $c.identity))) }}
{{ "export S3_ACCESS_KEY_ID='{{ .Data.data.access_key_id }}'" }}
{{ "export S3_SECRET_ACCESS_KEY='{{ .Data.data.secret_access_key }}'" }}
{{ "{{- end }}" }}
{{- end }}
{{- end }}
{{- /* Redis: every component (web uses its own logical DB). */}}
{{ printf "{{- with secret \"%s\" }}" (include "nexuscall.kvPath" (dict "root" $root "path" $g.redis.secretPath)) }}
{{ printf "export REDIS_URL='redis://:{{ .Data.data.password | urlquery }}@%s:%v/%v'" $g.redis.host $g.redis.port (ternary $root.Values.web.redisDb $g.redis.db (eq $c.kind "web")) }}
{{ "{{- end }}" }}
{{- if eq $c.kind "web" }}
{{- with $root.Values.web.secrets }}
{{ printf "{{- with secret \"%s\" }}" (include "nexuscall.kvPath" (dict "root" $root "path" .oidcClientSecret.path)) }}
{{ printf "export KC_WEB_BFF_CLIENT_SECRET='{{ .Data.data.%s }}'" .oidcClientSecret.field }}
{{ "{{- end }}" }}
{{ printf "{{- with secret \"%s\" }}" (include "nexuscall.kvPath" (dict "root" $root "path" .sessionKey.path)) }}
{{ printf "export NEXUS_SESSION_KEY='{{ .Data.data.%s }}'" .sessionKey.field }}
{{ "{{- end }}" }}
{{- end }}
{{- end }}
{{- range $var, $ref := $c.secretEnv }}
{{ printf "{{- with secret \"%s\" }}" (include "nexuscall.kvPath" (dict "root" $root "path" $ref.path)) }}
{{ printf "export %s='{{ .Data.data.%s }}'" $var $ref.field }}
{{ "{{- end }}" }}
{{- end }}
{{- end -}}

{{/*
Vault Agent Injector annotations (Kubernetes auth, role = the service's identity, in-memory secrets).
Call with (dict "root" $ "role" "crm-service" "template" "<env file template>" "prepopulateOnly" false)
*/}}
{{- define "nexuscall.vaultAnnotations" -}}
{{- $v := .root.Values.global.vault -}}
vault.hashicorp.com/agent-inject: "true"
vault.hashicorp.com/role: {{ .role | quote }}
vault.hashicorp.com/auth-path: {{ $v.authPath | quote }}
vault.hashicorp.com/service: {{ $v.addr | quote }}
vault.hashicorp.com/tls-secret: {{ $v.tlsSecret | quote }}
vault.hashicorp.com/ca-cert: {{ $v.caCertPath | quote }}
{{- if $v.agentImage }}
vault.hashicorp.com/agent-image: {{ $v.agentImage | quote }}
{{- end }}
vault.hashicorp.com/agent-service-account-token-volume-name: vault-token
vault.hashicorp.com/agent-run-as-user: {{ .root.Values.podSecurity.runAsUser | quote }}
vault.hashicorp.com/agent-run-as-group: {{ .root.Values.podSecurity.runAsGroup | quote }}
vault.hashicorp.com/agent-set-security-context: "true"
vault.hashicorp.com/agent-requests-cpu: {{ $v.agentResources.requests.cpu | quote }}
vault.hashicorp.com/agent-requests-mem: {{ $v.agentResources.requests.memory | quote }}
vault.hashicorp.com/agent-limits-cpu: {{ $v.agentResources.limits.cpu | quote }}
vault.hashicorp.com/agent-limits-mem: {{ $v.agentResources.limits.memory | quote }}
vault.hashicorp.com/agent-inject-secret-env: "env"
vault.hashicorp.com/agent-inject-perms-env: "0400"
vault.hashicorp.com/agent-inject-template-env: |
{{ .template | trim | indent 2 }}
{{- if .prepopulateOnly }}
vault.hashicorp.com/agent-pre-populate-only: "true"
{{- else }}
vault.hashicorp.com/agent-inject-token: "true"
vault.hashicorp.com/agent-cache-enable: "true"
vault.hashicorp.com/agent-cache-listener-port: "8200"
{{- end }}
{{- end -}}

{{/* Projected, audience-bound token used only by the Vault agent (never mounted into the app). */}}
{{- define "nexuscall.vaultTokenVolume" -}}
- name: vault-token
  projected:
    sources:
      - serviceAccountToken:
          path: token
          audience: vault
          expirationSeconds: 600
{{- end -}}

{{/*
Normalised component list: {items: [ {name, kind, identity, image, ...} ]}.
kind: service | web | worker. identity = the Vault role, ServiceAccount and broker user (workers reuse
their owning service's identity: same code, same least-privilege policy).
*/}}
{{- define "nexuscall.components" -}}
{{- $root := . -}}
{{- $items := list -}}
{{- range $name, $s := .Values.services }}
{{- if $s.enabled }}
{{- $env := merge (dict) (default (dict) $s.env) (default (dict) (get $root.Values.serviceEnv $name)) -}}
{{- $cmd := printf ". /vault/secrets/env && exec uvicorn %s.main:app --host 0.0.0.0 --port %v --proxy-headers --forwarded-allow-ips=%s --no-server-header --timeout-graceful-shutdown %v" $s.package $s.port $root.Values.global.forwardedAllowIps (sub (int $s.terminationGracePeriodSeconds) 5) -}}
{{- $items = append $items (dict "name" $name "kind" "service" "identity" $name "image" $s.image "package" $s.package "database" $s.database "port" $s.port "replicas" $s.replicas "resources" $s.resources "hpa" $s.hpa "grace" $s.terminationGracePeriodSeconds "public" $s.public "dependsOn" $s.dependsOn "infra" $s.infra "externalEgress" $s.externalEgress "env" $env "secretEnv" (default (dict) $s.secretEnv) "command" $cmd "tag" ($s.tag | default "") "digest" ($s.digest | default "") "healthPath" "/healthz" "readyPath" "/readyz") -}}
{{- end }}
{{- end }}
{{- if .Values.web.enabled }}
{{- $w := .Values.web -}}
{{- $items = append $items (dict "name" "web" "kind" "web" "identity" "web" "vaultRole" "web-bff" "image" $w.image "port" $w.port "replicas" $w.replicas "resources" $w.resources "hpa" $w.hpa "grace" $w.terminationGracePeriodSeconds "public" true "dependsOn" (list) "infra" (list "redis" "vault" "keycloak" "traefik") "externalEgress" false "env" (default (dict) $w.env) "secretEnv" (dict) "command" ". /vault/secrets/env && exec node apps/web/server.js" "tag" ($w.tag | default "") "digest" ($w.digest | default "") "healthPath" "/api/health" "readyPath" "/api/health") -}}
{{- end }}
{{- range $w := .Values.workers }}
{{- $o := merge (dict) (default (dict) (get $root.Values.workerOverrides $w.name)) $w -}}
{{- $owner := get $root.Values.services $w.owner -}}
{{- if and $owner $owner.enabled (ne (toString (get $o "enabled")) "false") }}
{{- $args := list -}}
{{- range $w.command }}{{- $args = append $args (squote .) -}}{{- end }}
{{- $env := merge (dict) (default (dict) $w.env) (default (dict) (get $root.Values.serviceEnv $w.owner)) -}}
{{- $items = append $items (dict "name" $w.name "kind" "worker" "identity" $w.owner "owner" $w.owner "image" $owner.image "package" $owner.package "database" $owner.database "replicas" $o.replicas "resources" $o.resources "hpa" (dict "enabled" false) "grace" $w.terminationGracePeriodSeconds "public" false "dependsOn" $owner.dependsOn "infra" $owner.infra "externalEgress" $owner.externalEgress "env" $env "secretEnv" (default (dict) $w.secretEnv) "queues" $w.queues "command" (printf ". /vault/secrets/env && exec %s" (join " " $args)) "tag" ($owner.tag | default "") "digest" ($owner.digest | default "")) -}}
{{- end }}
{{- end }}
{{- toYaml (dict "items" $items) -}}
{{- end -}}

{{/* Plain environment shared by every Python component (non-secret). */}}
{{- define "nexuscall.commonEnv" -}}
{{- $g := .Values.global -}}
ENV: {{ $g.env | quote }}
PROVIDER_MODE: {{ $g.providerMode | quote }}
LOG_LEVEL: {{ $g.logLevel | quote }}
OIDC_ISSUER: {{ $g.oidc.issuer | quote }}
OIDC_AUDIENCE: {{ $g.oidc.audience | quote }}
OIDC_JWKS_URL: {{ printf "%s/realms/%s/protocol/openid-connect/certs" (trimSuffix "/" $g.oidc.internalUrl) $g.oidc.realm | quote }}
OIDC_TOKEN_URL: {{ printf "%s/realms/%s/protocol/openid-connect/token" (trimSuffix "/" $g.oidc.internalUrl) $g.oidc.realm | quote }}
VAULT_ADDR: "http://127.0.0.1:8200"
VAULT_TOKEN_FILE: "/vault/secrets/token"
VAULT_KV_MOUNT: {{ $g.vault.kvMount | quote }}
OTEL_EXPORTER_OTLP_ENDPOINT: {{ $g.otel.endpoint | quote }}
S3_ENDPOINT_URL: {{ $g.objectStore.endpointUrl | quote }}
S3_PUBLIC_ENDPOINT_URL: {{ $g.objectStore.publicEndpointUrl | quote }}
S3_REGION: {{ $g.objectStore.region | quote }}
PUBLIC_BASE_URL: {{ $g.publicBaseUrl | quote }}
{{- end -}}

{{/* NetworkPolicy peer for an infrastructure endpoint. Call with (dict "ep" $endpoint) */}}
{{- define "nexuscall.endpointPeer" -}}
{{- if .ep.cidrs }}
{{- range .ep.cidrs }}
- ipBlock:
    cidr: {{ . }}
{{- end }}
{{- else }}
- namespaceSelector:
    matchLabels:
      kubernetes.io/metadata.name: {{ .ep.namespace }}
  podSelector:
    matchLabels:
      {{- toYaml .ep.podLabels | nindent 6 }}
{{- end }}
{{- end -}}
