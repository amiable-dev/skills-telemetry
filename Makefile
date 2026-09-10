.PHONY: install test validate skill-map up down smoke eval eval-dry
# `docker compose` (plugin) is absent on some installs; `docker-compose` (standalone) on others.
COMPOSE := $(shell docker compose version >/dev/null 2>&1 && echo docker compose || echo docker-compose)
install:      ; pip install -e ".[dev]" -q          # mise provides the venv; see mise.toml
test:         ; python -m pytest -q
validate:     ; python -m stdtel.manifest skills
skill-map:    ; (head -2 collector/copilot-skill-map.yaml; python -m stdtel.skillmap skills) > /tmp/m.yaml && mv /tmp/m.yaml collector/copilot-skill-map.yaml
up:           ; $(COMPOSE) -f deploy/docker-compose.yml up -d
down:         ; $(COMPOSE) -f deploy/docker-compose.yml down
smoke:        ; ./deploy/smoke.sh
eval:         ; python -m eval.run_eval --out eval/results.jsonl && tail -2 eval/results.jsonl
eval-dry:     ; python -m eval.run_eval --dry-run --out /tmp/eval.jsonl && cat /tmp/eval.jsonl
