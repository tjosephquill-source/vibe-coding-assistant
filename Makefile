.PHONY: dev analyze test lint

# Start the dev server (frontend + API on port 8000)
dev:
	venv/bin/uvicorn backend.server:app --reload --port 8000

# Run the analyzer standalone and pretty-print the graph JSON
analyze:
	venv/bin/python -m backend.analyzer mock_codebase

# Run the test suite
test:
	venv/bin/python -m pytest tests/ -v

# Run tests with coverage
test-cov:
	venv/bin/python -m pytest tests/ -v --tb=short

# Install dependencies
install:
	venv/bin/pip install -r requirements.txt

