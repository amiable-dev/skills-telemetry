.PHONY: install test validate skill-map up up-langfuse down smoke eval eval-dry load load-delivery load-watch
# `docker compose` (plugin) is absent on some installs; `docker-compose` (standalone) on others.
COMPOSE := $(shell docker compose version >/dev/null 2>&1 && echo docker compose || echo docker-compose)
install:      ; pip install -e ".[dev]" -q          # mise provides the venv; see mise.toml
test:         ; python -m pytest -q
validate:     ; python -m stdtel.manifest skills
skill-map:    ; (head -2 collector/copilot-skill-map.yaml; python -m stdtel.skillmap skills) > /tmp/m.yaml && mv /tmp/m.yaml collector/copilot-skill-map.yaml
up:           ; $(COMPOSE) -f deploy/docker-compose.yml up -d
up-langfuse:  ; mise run up-langfuse
# Every opt-in profile is named so `down` stops them too; without it they keep
# running and the network cannot be removed ("Resource is still in use").
down:         ; $(COMPOSE) -f deploy/docker-compose.yml --profile langfuse --profile loader down
smoke:        ; ./deploy/smoke.sh
# ADR-009 decision 7. A warehouse that depends on someone remembering to run
# the loader is a warehouse that is wrong: Tempo held four days of spans the
# warehouse had never seen, and the policy artefact in #21 had never been
# ingested at all. `load-watch` is the same thing on a loop for a dev machine;
# the compose `loader` profile is the deployed form.
# STDTEL_LOAD_REPO is what makes the delivery half runnable; without it only
# traces load. The policy artefact is fetched first because load_delivery takes
# it as a file — and because it had been produced on every CI run and collected
# by nothing since the job was written (#21).
load:         ; python -m warehouse.load_traces $(LOAD_ARGS) && $(MAKE) load-delivery
load-delivery:
	@if [ -z "$$STDTEL_LOAD_REPO" ]; then \
	  echo "load: set STDTEL_LOAD_REPO=owner/name to load delivery data and policy results"; \
	else \
	  python -m warehouse.fetch_policy_results --repo "$$STDTEL_LOAD_REPO" --out /tmp/stdtel-policy.jsonl || true; \
	  python -m warehouse.load_delivery --repo "$$STDTEL_LOAD_REPO" --policy-results /tmp/stdtel-policy.jsonl $(DELIVERY_ARGS); \
	fi
load-watch:   ; while true; do $(MAKE) load || true; sleep $${STDTEL_LOAD_INTERVAL:-900}; done
demo:         ; python -m warehouse.demo_seed
demo-clear:   ; python -m warehouse.demo_seed --clear
eval:         ; python -m eval.run_eval --out eval/results.jsonl && tail -2 eval/results.jsonl
eval-dry:     ; python -m eval.run_eval --dry-run --out /tmp/eval.jsonl && cat /tmp/eval.jsonl
