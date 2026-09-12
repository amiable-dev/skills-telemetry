# STD-LOG-001: never log request bodies, credentials or personal data.
# Evaluates file.log_calls — each logging call's argument list with brackets
# matched by the runner, so multi-line calls are graded whole.
package logging.no_pii

import rego.v1

forbidden := {
	"password", "passwd", "secret", "token", "api_key", "apikey",
	"authorization", "ssn", "credit_card", "card_number", "cvv", "email",
}

deny contains msg if {
	some file in input.files
	some call in file.log_calls
	some term in forbidden
	contains(lower(call), term)
	msg := sprintf("%s: log call references sensitive term %q", [file.path, term])
}

deny contains msg if {
	some file in input.files
	some call in file.log_calls
	regex.match(`(?is)(request\.(body|json|form)|await\s+request\.)`, call)
	msg := sprintf("%s: log call includes the request body", [file.path])
}
