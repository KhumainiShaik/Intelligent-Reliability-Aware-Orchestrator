{{- define "portable-workload.name" -}}
{{- default .Chart.Name .Values.workload.name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "portable-workload.fullname" -}}
{{- include "portable-workload.name" . -}}
{{- end -}}

{{- define "portable-workload.labels" -}}
app.kubernetes.io/name: {{ include "portable-workload.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: orchestrated-rollout-portability
workload.orchestrated.io/class: {{ .Values.workload.class | quote }}
workload.orchestrated.io/kind: {{ .Values.workload.kind | quote }}
{{- end -}}

{{- define "portable-workload.selectorLabels" -}}
app.kubernetes.io/name: {{ include "portable-workload.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
