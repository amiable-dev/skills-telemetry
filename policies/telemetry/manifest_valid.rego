# STD-TEL-001: a skill is only measurable if its manifest carries the contract.
# Graded over the same {"files":[{path, content}]} document as the other policies.
package telemetry.manifest_valid

import rego.v1

required_metadata := {"version", "standard_id", "policy_ids", "owner", "harness_support"}

skill_files contains file if {
	some file in input.files
	endswith(file.path, "SKILL.md")
}

deny contains msg if {
	some file in skill_files
	not startswith(file.content, "---")
	msg := sprintf("%s: no YAML front-matter", [file.path])
}

deny contains msg if {
	some file in skill_files
	some field in required_metadata
	not regex.match(sprintf(`(?m)^\s*%s\s*:`, [field]), file.content)
	msg := sprintf("%s: missing contract field %q", [file.path, field])
}

# Agent Skills permits only six top-level keys; ours must ride under metadata:
deny contains msg if {
	some file in skill_files
	some field in required_metadata
	regex.match(sprintf(`(?m)^%s\s*:`, [field]), file.content)
	msg := sprintf("%s: %q is a top-level key; it belongs under metadata:", [file.path, field])
}
