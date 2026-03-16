.PHONY: dev analyze test

# Start the dev server (frontend + API on port 8000)
dev:
	venv/bin/uvicorn backend.server:app --reload --port 8000

# Run the analyzer standalone and pretty-print the graph JSON
analyze:
	venv/bin/python -m backend.analyzer mock_codebase

# Install dependencies
install:
	venv/bin/pip install -r requirements.txt

