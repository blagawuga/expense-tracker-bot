.PHONY: setup dev up down logs backfill shell

setup:
	cp -n .env.example .env || true
	pip install -r requirements.txt

dev:
	DB_PATH=expenses.db python bot.py

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f

backfill:
	docker compose run --rm bot python backfill.py

shell:
	docker compose exec bot bash
