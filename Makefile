.PHONY: help setup setup-research research mlflow mlflow-smoke check check-db api contracts profile frontend frontend-build compose compose-config compose-server-config
.DEFAULT_GOAL := help

# Full checks must include optional data/ML dependencies: no silent importorskip.
CHECK_RUN = uv run --locked --group platform --group train --group research
RESEARCH_RUN = uv run --locked --group train --group research
SEQUENCE_RUN = uv run --locked --group train --group sequence

.PHONY: check-sequence sequence-smoke

# Explicit optional torch suite lives outside the default pytest testpaths.
check-sequence:
	$(SEQUENCE_RUN) pytest data-science/tests/test_sequence_data.py data-science/tests/test_episode_research.py data-science/sequence-tests

sequence-smoke:
	$(SEQUENCE_RUN) python -m infra_pulse_research.sequence smoke --output data-science/artifacts/sequence-v1/smoke

help:
	@echo "setup / setup-research: install API / local research environment"
	@echo "api / frontend: start development servers in separate terminals"
	@echo "research / mlflow / mlflow-smoke: local research tools"
	@echo "check / check-db / contracts / frontend-build: verify, run PostgreSQL integration, regenerate contracts, build UI"
	@echo "  check-db runs the app code as the runtime role infra_pulse_app (INFRA_TEST_DB_ROLE=owner: as the DSN user)"
	@echo "check-sequence / sequence-smoke: optional local torch research (group sequence, not in CI)"
	@echo "compose / compose-config: start local stack / validate configuration"
	@echo "compose-server-config: validate the stand override with the .env template (deploy/README.md)"

setup:
	uv sync --locked

setup-research:
	uv sync --locked --group train --group research

research:
	$(RESEARCH_RUN) jupyter lab data-science/notebooks

mlflow:
	$(RESEARCH_RUN) python data-science/mlflow/server.py

mlflow-smoke:
	$(RESEARCH_RUN) python data-science/mlflow/smoke.py

check:
	$(CHECK_RUN) ruff check .
	$(CHECK_RUN) ruff format --check .
	$(CHECK_RUN) python -c "import catboost, duckdb, pyarrow, sklearn, mlflow, sqlalchemy, psycopg, jupyterlab; print('Full check dependencies OK')"
	$(CHECK_RUN) pytest

check-db:
	@test -n "$$INFRA_TEST_RECEIVED_DSN" || (echo "INFRA_TEST_RECEIVED_DSN must point to a disposable PostgreSQL database" >&2; exit 1)
	@test -n "$$INFRA_TEST_REPLAY_DSN" || (echo "INFRA_TEST_REPLAY_DSN must point to a disposable PostgreSQL database" >&2; exit 1)
	$(CHECK_RUN) pytest -q backend/tests/test_received_batch.py backend/tests/test_replay_coverage.py $(wildcard backend/tests/test_*_db.py)

api:
	uv run --locked uvicorn infra_pulse_backend.api.app:app --reload --host 127.0.0.1 --port 8000

contracts:
	uv run --locked python tools/export_contracts.py

profile:
	$(RESEARCH_RUN) python data-science/scripts/profile_data.py --source "$(SOURCE)" --output data-science/reports/local/profile.json

frontend:
	cd frontend && npm run dev

frontend-build:
	cd frontend && npm run build

compose:
	docker compose up --build

compose-config:
	docker compose config --quiet

compose-server-config:
	docker compose -f compose.yaml -f deploy/compose.server.yaml --env-file deploy/.env.server.example --profile tools --profile received config --quiet
