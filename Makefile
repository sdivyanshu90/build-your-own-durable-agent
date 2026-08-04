.PHONY: install lock-check format format-check lint typecheck test coverage security docs-check migration-test build demo api clean

install:
	python -m pip install --constraint requirements.lock -e '.[dev,pdf,postgres]'

lock-check:
	python scripts/validate_dependency_lock.py

format:
	ruff format .
	ruff check --fix .

format-check:
	ruff format --check .

lint:
	ruff check .

typecheck:
	mypy src

test:
	pytest -m 'not performance'

coverage:
	pytest -m 'not performance' --cov=src/durable_agent --cov-report=term-missing --cov-report=xml

security:
	bandit -c pyproject.toml -r src
	pip-audit

docs-check:
	python scripts/validate_docs.py

migration-test:
	python scripts/validate_migrations.py

build:
	python -m build

demo:
	python scripts/run_demo.py --workspace .demo

api:
	uvicorn durable_agent.api.app:create_app --factory --host 127.0.0.1 --port 8000

clean:
	python scripts/clean.py
