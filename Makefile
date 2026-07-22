# Makefile for Umba FX Engine

# Load environment variables from .env if it exists
ifneq (,$(wildcard .env))
    include .env
    export
endif

# Fallback default password if not defined in .env
POSTGRES_PASSWORD ?= fx_password

.PHONY: help build up down restart test load-test logs status clean

help:
	@echo "Available commands:"
	@echo "  make build       - Build the Docker images"
	@echo "  make up          - Start the application and database in the background"
	@echo "  make down        - Stop and remove all containers"
	@echo "  make restart     - Restart the application"
	@echo "  make test        - Run the test suite inside Docker"
	@echo "  make load-test   - Run the Locust load test inside Docker"
	@echo "  make logs        - Tail logs from all containers"
	@echo "  make status      - Show status of the containers"
	@echo "  make clean       - Remove Docker volumes and temporary files"

build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down

restart:
	docker compose restart

test:
	docker compose run --rm -e FX_TEST_DATABASE_URL=postgresql://fx_user:$(POSTGRES_PASSWORD)@postgres:5432/fx_engine_test fx-engine pytest

load-test:
	docker compose run --rm fx-engine locust -f scripts/load_test.py --headless -u 20 -r 2 -t 15s --host http://localhost:8000

logs:
	docker compose logs -f

status:
	docker compose ps

clean:
	docker compose down -v
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -exec rm -rf {} +
