.PHONY: dev test lint migrate upgrade seed

dev:
	docker compose -f docker-compose.dev.yml up -d
	cd api && uvicorn app.main:app --reload

test:
	cd api && pytest

lint:
	cd api && ruff check . && ruff format --check .

migrate:
	cd api && alembic revision --autogenerate -m "$(m)"

upgrade:
	cd api && alembic upgrade head

seed:
	cd api && python ../scripts/seed.py
