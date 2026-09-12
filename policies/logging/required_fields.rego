# STD-LOG-001: every log line carries the platform's required fields.
# Input is {"files": [{"path": ..., "content": ...}]} built by eval/run_eval.py.
package logging.required_fields

import rego.v1

required := {"timestamp", "level", "service", "trace_id", "span_id", "message"}

log_call := `(?i)\b(logger|logging|log|LOG)\s*\.\s*(debug|info|warn|warning|error|critical|exception)\s*\(`

emits_logs contains file if {
	some file in input.files
	regex.match(log_call, file.content)
}

# A solution that adds no logging at all must not pass by vacuous truth.
deny contains msg if {
	count(emits_logs) == 0
	msg := "no log emission found; the task requires request logging"
}

deny contains msg if {
	some file in emits_logs
	some field in required
	not mentions(file.content, field)
	msg := sprintf("%s: required log field %q is never set", [file.path, field])
}

# the field name appearing as a dict key, kwarg, or structured-logging attribute
mentions(content, field) if regex.match(sprintf(`(?i)["']?%s["']?\s*[:=]`, [field]), content)
