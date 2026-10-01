.PHONY: help install setup test e2e run clean docker-up docker-down docker-logs ontology-ddl graph-up

help:
	@echo "SimplerToday - Available Commands"
	@echo "=================================="
	@echo "make install      - Install dependencies"
	@echo "make setup        - Complete setup (venv + deps + services)"
	@echo "make test         - Run setup tests"
	@echo "make run          - Run the application"
	@echo "make clean        - Clean temporary files"
	@echo "make docker-up    - Start Docker services (Qdrant, Redis)"
	@echo "make docker-down  - Stop Docker services"
	@echo "make docker-logs  - View Docker service logs"

install:
	@echo "Installing dependencies..."
	pip install -r requirements.txt

setup:
	@echo "Setting up SimplerToday..."
	@if [ ! -d "venv" ]; then \
		echo "Creating virtual environment..."; \
		python3 -m venv venv; \
	fi
	@echo "Installing dependencies..."
	@bash -c "source venv/bin/activate && pip install -r requirements.txt"
	@if [ ! -f ".env" ]; then \
		echo "Creating .env file from template..."; \
		cp env.example .env; \
		echo "⚠️  Please edit .env with your configuration!"; \
	fi
	@mkdir -p logs logs/audit data/workspaces
	@echo "✅ Setup complete!"

test:
	@echo "Running unit tests and offline verification suites..."
	python -m pytest -q tests
	@for f in verify_logging.py verify_rag.py verify_run_control.py verify_document_reload.py verify_objective_change.py verify_workspace_share.py verify_fact_corrections.py; do \
		echo "== $$f"; python $$f | tail -1 || exit 1; \
	done

e2e:
	@echo "Pre-deploy end-to-end run against a local stand-in backend (real, billed Gemini calls)..."
	python scripts/e2e_local_backend.py

ontology-ddl:
	@echo "Generating the Neo4j ontology schema..."
	python -m app.ontology.neo4j_ddl

graph-up:
	@echo "Starting Neo4j (requires NEO4J_PASSWORD in .env)..."
	docker-compose --profile graph up -d neo4j

run:
	@echo "Starting SimplerToday..."
	python -m app.main

clean:
	@echo "Cleaning temporary files..."
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	find . -type f -name "*.log" -delete 2>/dev/null || true
	@echo "✅ Cleaned!"

docker-up:
	@echo "Starting Docker services..."
	docker-compose up -d
	@echo "Waiting for services to be ready..."
	@sleep 3
	@docker-compose ps

docker-down:
	@echo "Stopping Docker services..."
	docker-compose down

docker-logs:
	@echo "Docker service logs:"
	docker-compose logs -f

lint:
	@echo "Running linters..."
	@if command -v pylint >/dev/null 2>&1; then \
		pylint app/ || true; \
	else \
		echo "pylint not installed. Skipping..."; \
	fi

format:
	@echo "Formatting code..."
	@if command -v black >/dev/null 2>&1; then \
		black app/; \
	else \
		echo "black not installed. Skipping..."; \
	fi

dev: docker-up
	@echo "Starting development environment..."
	@make run

