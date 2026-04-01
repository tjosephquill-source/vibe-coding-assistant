# VibeCodingAssistant

**Interactive Architecture Graph Tool** — Transform any codebase into a navigable, interactive architecture graph with AI-powered descriptions, pattern detection, and a guided walkthrough experience.

<p align="center">
  <em>Analyse → Visualise → Understand</em>
</p>

---

## Features

- **Multi-language static analysis** — Python (AST), JavaScript/TypeScript (regex), and HTML parsing
- **Interactive graph visualisation** — D3.js force-directed graph with zoom, pan, search, and drill-down
- **AI-powered descriptions** — Hierarchical LLM-generated descriptions for every node in the graph
- **Architecture insights** — Automatic detection of circular dependencies, god classes, orphan modules, hub classes, and layer violations
- **Guided walkthroughs** — AI-narrated architecture tours at high, medium, and low detail levels, with optional text-to-speech
- **LLM chat** — Ask questions about the codebase with tool-use (read files, search code, navigate graph)
- **Project management** — Register multiple codebases and switch between them
- **IDE-style UI** — Resizable split panels, tab groups, floating windows, and layout persistence
- **Code editor** — View source code for any node directly in the UI
- **Node enrichment** — AI-classified content icons (database, auth, network, etc.)

## Quick Start

### Prerequisites

- Python 3.10+
- An OpenAI API key (for AI features)

### Installation

```bash
# Clone the repository
git clone https://github.com/your-org/vibe-coding-assistant.git
cd vibe-coding-assistant

# Create and activate a virtual environment
python -m venv venv
source venv/bin/activate  # macOS/Linux
# venv\Scripts\activate   # Windows

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env and add your OPENAI_API_KEY
```

### Running

```bash
# Start the development server
make dev
# Or directly:
uvicorn backend.server:app --reload --port 8000
```

Open [http://localhost:8000](http://localhost:8000) in your browser.

### First Use

1. The app opens with a project picker — click **"Add Project"** and select a directory containing source code
2. The analyzer scans all Python, JS/TS, and HTML files and builds an architecture graph
3. Explore the graph: zoom, pan, click nodes to see details, double-click to drill down
4. Use the toolbar buttons to generate **AI descriptions**, view **insights**, or start a **walkthrough**

## Architecture

```
vibe-coding-assistant/
├── backend/
│   ├── server.py          # FastAPI app — all API endpoints
│   ├── analyzer.py         # Static analysis engine (Python AST + JS/TS regex)
│   ├── descriptors.py      # Hierarchical LLM description generator
│   ├── insights.py         # Pattern detection (cycles, god classes, etc.)
│   ├── llm_chat.py         # LLM chat with tool-use (OpenAI function calling)
│   └── projects.py         # Project registry and persistence
├── frontend/
│   ├── index.html          # Single-page app (vanilla JS + D3.js)
│   └── wm.js              # Window manager (resizable panels, tabs, floating)
├── tests/
│   ├── test_analyzer.py    # Static analysis & graph model tests
│   ├── test_insights.py    # Pattern detection rule tests
│   ├── test_server.py      # API endpoint tests
│   ├── test_llm_chat.py    # Chat tool dispatch & file operation tests
│   ├── test_projects.py    # Project registry tests
│   ├── test_descriptors.py # Hierarchy building & cache tests
│   └── fixtures/           # Sample Python & JS codebases for testing
├── PLAN.md                 # Detailed architecture plan
├── TASKS.md                # Task breakdown by milestone
├── GRAPH_RULES.md          # Graph construction rules documentation
└── check_graph.py          # CLI graph analysis utility
```

## API Endpoints

### Projects

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/projects` | List all projects and current active ID |
| `POST` | `/api/projects` | Create a project `{name, path}` |
| `POST` | `/api/projects/select` | Switch active project `{project_id}` |
| `DELETE` | `/api/projects/{id}` | Delete a project |

### Analysis & Graph

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/analyze` | Analyse a codebase |
| `GET` | `/api/graph` | Get the architecture graph (supports `?abstract=true`) |
| `POST` | `/api/graph/refresh` | Re-analyse without clearing descriptions |
| `POST` | `/api/reanalyze` | Full re-analysis (clears all caches) |
| `GET` | `/api/insights` | Run pattern detection and return insights |

### Files & Browsing

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/files` | Directory tree of active project |
| `GET` | `/api/source?path=...` | Source code of a file |
| `GET` | `/api/browse?path=...` | Browse filesystem directories |
| `POST` | `/api/mkdir` | Create a new directory |
| `GET` | `/api/fs/checksum` | File change detection checksum |

### AI Features

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/describe` | Start hierarchical description generation |
| `GET` | `/api/describe` | Get cached descriptions |
| `GET` | `/api/describe/status` | Description generation progress |
| `POST` | `/api/node-description` | Generate a single node description |
| `POST` | `/api/preview` | Generate architecture walkthrough |
| `POST` | `/api/preview/audio` | Text-to-speech for walkthrough narration |
| `POST` | `/api/enrich` | Classify nodes into content categories |
| `POST` | `/api/chat` | Stream LLM chat response with tool-use |
| `GET` | `/api/chat/models` | List available LLM models |

### Layout Persistence

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/positions` | Get cached node positions |
| `POST` | `/api/positions` | Save node positions |
| `GET` | `/api/layout` | Get window layout state |
| `POST` | `/api/layout` | Save window layout state |

## Configuration

Environment variables (set in `.env`):

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | — | OpenAI API key (required for AI features) |
| `LLM_MODEL` | `gpt-5.4-mini` | LLM model for descriptions, chat, and previews |

## Development

```bash
# Run the dev server with auto-reload
make dev

# Run the analyzer on a directory (CLI)
make analyze

# Install dependencies
make install

# Run tests
make test
```

## How It Works

### Static Analysis

The analyzer walks the target codebase and extracts:
- **Python**: Full AST parsing — classes, methods, inheritance, imports, function calls, type annotations, decorators
- **JavaScript/TypeScript**: Regex-based extraction — classes, methods, inheritance, imports, instantiation
- **HTML**: Inline `<script>` blocks are extracted and parsed as JS

### Graph Construction

Nodes represent classes and methods. Edges represent relationships:
- `contains` — structural parent→child (class→method)
- `inherits` — class extends another class
- `calls` — method invokes another method
- `imports` — module imports another module
- `instantiates` — code creates an instance of a class
- `uses_type` — type annotation references a class

### Abstract View

The abstract view groups related classes using heuristic classification:
1. Exception/Error classes → grouped together
2. Events/Signals → grouped
3. Tests → grouped
4. Models/Schemas → grouped
5. Config/Settings → grouped
6. Services/Handlers → grouped
7. Utilities/Helpers → grouped
8. Remaining classes → grouped by file

Connected components with strong internal cohesion are collapsed into "island chains".

### Hierarchical Descriptions

The description generator works bottom-up:
1. Reads source code for leaf nodes (methods/classes)
2. Generates descriptions via GPT
3. Synthesises parent descriptions from children
4. Caches everything keyed by content hash — only changed nodes are regenerated

## License

MIT
