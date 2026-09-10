.PHONY: install test validate skill-map up down eval-dry
install:      ; pip install -e ".[dev]" --break-system-packages -q
test:         ; python -m pytest -q
validate:     ; python -m stdtel.manifest skills
skill-map:    ; (head -2 collector/copilot-skill-map.yaml; python -m stdtel.skillmap skills) > /tmp/m.yaml && mv /tmp/m.yaml collector/copilot-skill-map.yaml
up:           ; docker compose -f deploy/docker-compose.yml up -d
down:         ; docker compose -f deploy/docker-compose.yml down
eval-dry:     ; python -m eval.run_eval --dry-run --out /tmp/eval.jsonl && cat /tmp/eval.jsonl
