.PHONY: dev-up test test-unit lint lint-python lint-frontend build deploy verify backup restore

PYTHON ?= python
NPM ?= npm
COMPOSE = docker compose -f infrastructure/compose/compose.base.yml -f infrastructure/compose/compose.dev.yml

dev-up:
	$(COMPOSE) up -d

test:
	@test -n "$(TEST_DATABASE_URL)" || (echo "TEST_DATABASE_URL is required" && exit 2)
	$(PYTHON) -m pytest backend/tests/unit backend/tests/integration backend/tests/guards

test-unit:
	$(PYTHON) -m pytest backend/tests/unit

lint: lint-python lint-frontend

lint-python:
	$(PYTHON) -m ruff check backend
	$(PYTHON) -m mypy backend/app backend/tests

lint-frontend:
	cd frontend && $(NPM) run lint
	cd frontend && $(NPM) run typecheck

build:
	docker compose -f infrastructure/compose/compose.base.yml build

deploy:
	@test -n "$(HOST)" || (echo "HOST is required" && exit 2)
	./deploy/bootstrap.sh --host "$(HOST)"

verify:
	@test -n "$(HOST)" || (echo "HOST is required" && exit 2)
	ssh "$(HOST)" /opt/lingsway/current/deploy/lib/70_verify.sh

backup:
	@test -n "$(HOST)" || (echo "HOST is required" && exit 2)
	ssh "$(HOST)" /opt/lingsway/current/ops/backup/backup.sh

restore:
	@test -n "$(ARCHIVE)" || (echo "ARCHIVE is required" && exit 2)
	./ops/backup/restore.sh "$(ARCHIVE)"
