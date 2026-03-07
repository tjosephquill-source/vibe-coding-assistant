# VibeCodingAssistant — Interactive Architecture Graph Tool

## Plan & Blueprint

> Transform any codebase into a navigable, interactive architecture graph. A Python backend performs multi-language static analysis and LLM summarisation, exposes a graph via a FastAPI server, and a React + React Flow frontend renders an explorable node-edge map. Optional runtime trace ingestion and IDE plugins allow real-time, augmented code navigation.

---

## 1. Tech Stack

| Layer | Choice | Rationale |
|---|---|---|
| **Language** | Python 3.10+ | Mature AST/parsing ecosystem, LLM SDKs |
| **Static analysis** | `tree-sitter` via `py-tree-sitter` + language grammars (Python, JS/TS, Java, Go, C#); Python's built-in `ast` module as fast-path for `.py` files | Multi-language, incremental, battle-tested |
| **Graph storage** | `networkx` in-memory for small/medium; `Neo4j` (via `neo4j` driver) for persistent/large graphs | NetworkX for prototyping, Neo4j for scale |
| **Backend API** | FastAPI + Uvicorn | Async, fast, auto-docs |
| **Task queue** | Celery + Redis (background parsing, LLM calls) | Parallel & incremental processing |
| **LLM** | OpenAI API (`openai` SDK, GPT-4o-mini for summaries) with `litellm` adapter for provider portability | Cost-effective, swappable |
| **Frontend** | React 18 + TypeScript, React Flow for the graph canvas, TailwindCSS for UI | React Flow has built-in zoom/pan/minimap/grouping |
| **Bundler** | Vite | Fast dev experience |
| **IDE extensions** | VS Code extension (TypeScript), JetBrains plugin (Kotlin) | Two largest IDE markets |
| **Runtime traces** | OpenTelemetry Collector → OTLP JSON export → ingestion endpoint | Vendor-neutral standard |

---

## 2. Project File/Folder Structure

```
VibeCodingAssistant/
├── backend/
│   ├── main.py                     # FastAPI entrypoint
│   ├── config.py                   # Settings (Pydantic BaseSettings)
│   ├── api/
│   │   ├── routes_graph.py         # /graph, /graph/{node_id}
│   │   ├── routes_analysis.py      # /analyze, /analyze/status
│   │   ├── routes_search.py        # /search
│   │   └── routes_traces.py        # /traces/ingest
│   ├── analysis/
│   │   ├── engine.py               # Orchestrator: walk files → parse → build graph
│   │   ├── parsers/
│   │   │   ├── base.py             # Abstract LanguageParser class
│   │   │   ├── python_parser.py    # ast + tree-sitter for Python
│   │   │   ├── js_ts_parser.py     # tree-sitter JS/TS grammars
│   │   │   ├── java_parser.py      # tree-sitter Java grammar
│   │   │   └── registry.py         # Maps file extension → parser
│   │   ├── graph_builder.py        # Converts parsed symbols → graph nodes/edges
│   │   └── incremental.py          # File-watcher + delta re-parse logic
│   ├── graph/
│   │   ├── models.py               # Pydantic schemas: Node, Edge, Graph
│   │   ├── store.py                # Abstract GraphStore interface
│   │   ├── networkx_store.py       # NetworkX implementation
│   │   └── neo4j_store.py          # Neo4j implementation
│   ├── llm/
│   │   ├── summariser.py           # Summarise nodes/edges via LLM
│   │   ├── prompts.py              # Prompt templates
│   │   └── cache.py                # SQLite/diskcache for LLM results
│   ├── traces/
│   │   ├── ingest.py               # Parse OTLP JSON / OpenTelemetry spans
│   │   └── merge.py                # Overlay runtime data onto static graph
│   ├── patterns/
│   │   ├── detector.py             # Pattern / anti-pattern detection engine
│   │   └── rules.py                # Individual rules (circular deps, god class…)
│   ├── tasks.py                    # Celery tasks (analysis, LLM, trace ingestion)
│   └── tests/
│       ├── test_parsers.py
│       ├── test_graph_builder.py
│       ├── test_summariser.py
│       └── fixtures/               # Sample code snippets per language
├── frontend/
│   ├── package.json
│   ├── vite.config.ts
│   ├── tsconfig.json
│   ├── index.html
│   ├── src/
│   │   ├── App.tsx
│   │   ├── main.tsx
│   │   ├── api/
│   │   │   └── client.ts           # Axios/fetch wrapper to backend
│   │   ├── components/
│   │   │   ├── GraphCanvas.tsx      # React Flow wrapper
│   │   │   ├── NodeDetail.tsx       # Side panel: summary, metrics, code link
│   │   │   ├── SearchBar.tsx
│   │   │   ├── FilterPanel.tsx      # Filter by module, type, layer
│   │   │   ├── MiniMap.tsx
│   │   │   └── ClusterGroup.tsx     # Collapsible module/package groups
│   │   ├── hooks/
│   │   │   ├── useGraph.ts
│   │   │   └── useSearch.ts
│   │   ├── layouts/
│   │   │   └── algorithms.ts       # Dagre, ELK, force-directed configs
│   │   ├── types/
│   │   │   └── graph.ts            # TypeScript mirrors of backend models
│   │   └── utils/
│   │       └── colors.ts           # Color scheme per node/edge type
│   └── public/
├── extensions/
│   ├── vscode/                     # VS Code extension scaffold
│   │   ├── package.json
│   │   ├── src/extension.ts
│   │   └── README.md
│   └── jetbrains/                  # JetBrains plugin scaffold
│       ├── build.gradle.kts
│       └── src/…
├── pyproject.toml                  # Backend deps, project metadata
├── requirements.txt                # Pinned backend deps
├── Makefile                        # dev, lint, test, build shortcuts
├── docker-compose.yml              # Redis, Neo4j, backend, frontend
├── .env.example
├── PLAN.md                         # This file
└── README.md
```

---

## 3. Graph Data Model

### Node Schema

```python
class NodeKind(str, Enum):
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    VARIABLE = "variable"
    SERVICE = "service"
    PACKAGE = "package"

class Node(BaseModel):
    id: str                          # Deterministic hash of (file_path, qualified_name)
    kind: NodeKind
    name: str                        # Short name, e.g. "UserService"
    qualified_name: str              # Full dotted path, e.g. "app.services.user_service.UserService"
    file_path: str                   # Relative to repo root
    line_start: int
    line_end: int
    language: str                    # "python", "typescript", "java", etc.
    metrics: dict                    # {loc, complexity, fan_in, fan_out}
    summary: Optional[str] = None   # LLM-generated summary
    cluster: str                    # Parent package/module for grouping
    tags: list[str] = []            # e.g. ["controller", "entrypoint"]
    runtime: Optional[dict] = None  # {call_count, avg_latency_ms, error_rate}
```

### Edge Schema

```python
class EdgeKind(str, Enum):
    CALLS = "calls"
    IMPORTS = "imports"
    INHERITS = "inherits"
    IMPLEMENTS = "implements"
    SENDS_DATA_TO = "sends_data_to"
    INSTANTIATES = "instantiates"
    DECORATES = "decorates"

class Edge(BaseModel):
    source: str                     # Node ID
    target: str                     # Node ID
    kind: EdgeKind
    weight: float = 1.0             # 1.0 for static; runtime call frequency otherwise
    metadata: dict = {}             # {line, argument_types, is_async}
    summary: Optional[str] = None   # LLM-generated label
```

Serialised as JSON for the API; stored as `networkx.DiGraph` node/edge attributes or Neo4j properties.

---

## 4. Static Analysis Engine

### Step-by-step Pipeline

1. **File discovery**: Walk the target repo, respect `.gitignore` via `pathspec`. Map each file extension to a parser via `registry.py`.

2. **Parsing (per-file)**: Each parser returns a list of `SymbolDef` (name, kind, line range, docstring, decorators) and a list of `SymbolRef` (name, kind, line, resolved target if possible).
   - **Python**: Use `ast.parse()` + `ast.NodeVisitor` for speed; fall back to `tree-sitter` for partial/broken files.
   - **JS/TS**: Use `tree-sitter-javascript` / `tree-sitter-typescript` grammars. Extract `import`, `export`, `class`, `function`, arrow functions, `require()`.
   - **Java**: Use `tree-sitter-java`. Extract `import`, `class`, `interface`, `method`, annotations.

3. **Name resolution**: After all files are parsed, build a symbol table (`qualified_name → SymbolDef`). Resolve each `SymbolRef` to its target using import paths. For unresolved references, mark edges as `tentative`.

4. **Graph construction** (`graph_builder.py`): Convert `SymbolDef` → `Node`, resolved `SymbolRef` pairs → `Edge`. Compute `fan_in`/`fan_out` metrics. Group nodes into `cluster` by package path.

5. **Incremental updates** (`incremental.py`): Use `watchfiles` to monitor the repo. On change, re-parse only changed files, diff the symbol table, and patch the graph (remove stale nodes/edges, add new ones). Store file content hashes to detect changes.

### Parser Abstract Interface

```python
class LanguageParser(ABC):
    @abstractmethod
    def parse_file(self, file_path: Path, content: str) -> ParseResult:
        """Return ParseResult(symbols: list[SymbolDef], references: list[SymbolRef])"""
        ...

    @abstractmethod
    def supported_extensions(self) -> list[str]:
        """Return e.g. ['.py', '.pyi']"""
        ...
```

---

## 5. Runtime Trace Integration

1. **Ingestion endpoint** `POST /traces/ingest` in `routes_traces.py`: Accept OTLP JSON span batches or a simplified `{caller, callee, timestamp, duration_ms, status}` format.

2. **Span → edge mapping** in `traces/merge.py`: Match `span.operation_name` (e.g., `UserService.get_user`) to graph node `qualified_name` using fuzzy matching and a configurable mapping table. Aggregate per-edge: `call_count`, `p50/p99 latency`, `error_rate`.

3. **Node augmentation**: Attach runtime stats to `node.runtime` dict. Frontend renders heatmap colouring (e.g., hot paths in red, cold paths in blue).

4. **Instrumentation helpers**: Provide a lightweight Python decorator `@trace` that emits spans to the local collector, so users can instrument their code with minimal effort. For JS/TS, provide an Express/Koa middleware example.

---

## 6. LLM-Based Summarisation

### Prompt Design (`llm/prompts.py`)

- **Node prompt**: `"Summarise this {kind} in 1–2 sentences. Focus on purpose and key behaviour.\n\n```{language}\n{source_code}\n```"`
- **Edge prompt**: `"Describe the relationship between '{source.name}' and '{target.name}' in one sentence given: {source calls target at line X, passing args…}"`

### Token Management

- Truncate source code to 3,000 tokens (≈ 12k chars).
- For large classes, send only the method signatures + docstrings.
- Use `tiktoken` for accurate token counting.

### Batching & Cost

- Use GPT-4o-mini ($0.15/1M input tokens).
- Batch summarisation via Celery tasks — up to 50 nodes per task.
- Estimated cost for a 500-class codebase: ~$0.30.

### Caching (`llm/cache.py`)

- Key = `sha256(source_code_snippet + prompt_template_version)`.
- Store in SQLite via `diskcache` library.
- Invalidate when source code changes (detected by incremental parser).
- TTL: 30 days.

### Fallback

- If LLM is unavailable, fall back to extracting the first docstring/comment from the symbol.

---

## 7. Interactive Visualisation Frontend

### Graph Canvas (`GraphCanvas.tsx`)

- React Flow with custom node components styled by `node.kind` (icon + colour).
- Edges styled by `edge.kind` (solid for calls, dashed for imports, dotted for data flow).
- Animated edges for runtime hot paths.

### Layout

- Use `@dagrejs/dagre` for hierarchical layout (default) and `elkjs` for layered/force-directed toggle.
- Compute layout in a Web Worker to avoid UI jank.

### Grouping / Clustering

- React Flow's `GroupNode` to represent packages/modules.
- Collapse/expand groups.
- Double-click a group node to "zoom into" that subgraph.

### Interaction Features

| Feature | Component | Details |
|---|---|---|
| **Search** | `SearchBar.tsx` | Fuzzy search over node names via Fuse.js; highlights matching node on canvas |
| **Filter** | `FilterPanel.tsx` | Checkboxes for node kinds, edge kinds, modules. Slider for "minimum fan-out" to hide leaves |
| **Detail panel** | `NodeDetail.tsx` | On node click, show: LLM summary, metrics, file path (clickable link), source code preview (syntax-highlighted via `react-syntax-highlighter`), runtime stats if available |
| **Minimap** | `MiniMap.tsx` | React Flow's built-in `<MiniMap>` component |

### API Integration (`api/client.ts`)

- Fetch `/graph` on load (paginated: top-level clusters first, expand on demand).
- WebSocket channel for incremental graph updates when backend re-analyses.

---

## 8. Code Navigation & IDE Integration

### Web UI

Each node's detail panel shows a `file_path:line_start` link. If running locally, open via:
- `vscode://file/{abs_path}:{line}` for VS Code
- `jetbrains://open?file={abs_path}&line={line}` for JetBrains

### VS Code Extension (`extensions/vscode/`)

- Command `VibeCodingAssistant: Show Graph` opens a WebView panel that embeds the React app pointed at the local FastAPI server.
- `CodeLens` provider: above each class/function definition, show a clickable "View in Graph" link that messages the WebView to focus that node.
- On node click inside the WebView, send a message back to the extension host to open the file at the line.

### JetBrains Plugin (`extensions/jetbrains/`)

- JCEF browser panel embedding the React app.
- `LineMarkerProvider` for "View in Graph" gutter icons.
- Identical bidirectional navigation via the plugin's messaging bridge.

---

## 9. Handling Large Codebases

| Strategy | Details |
|---|---|
| **Incremental parsing** | Only re-parse changed files (file hash comparison). Store parsed symbol data in a SQLite sidecar DB so restarts don't require full re-parse. |
| **Hierarchical lazy loading** | API returns top-level package clusters first (`/graph?depth=1`). Frontend expands clusters on demand (`/graph/{cluster_id}/children`). Keeps initial payload < 500 nodes. |
| **Background processing** | Celery workers parse files in parallel (one task per file). LLM summarisation runs as low-priority background tasks. |
| **Graph DB for scale** | For repos > 10k files, switch to Neo4j backend which handles traversal queries natively. The abstract `GraphStore` interface in `store.py` makes this a config toggle. |
| **Frontend virtualisation** | React Flow handles 10k+ nodes natively. For extreme cases, render only nodes in viewport + a 1-screen buffer. |

---

## 10. Pattern Detection & Insights

Implemented in `backend/patterns/` with a rule engine:

| Rule | Algorithm | Threshold |
|---|---|---|
| **Circular dependency** | Tarjan's SCC on IMPORTS subgraph (`networkx.strongly_connected_components`) | Cycles > 1 node |
| **God class** | Metric check | `loc > 500` AND `fan_out > 20` |
| **Orphan module** | Connectivity check | `fan_in == 0` AND `fan_out == 0` (excluding tagged entrypoints) |
| **Layer violation** | User-defined layers + edge direction check | Any edge that skips or reverses layers |
| **Architecture inference** | Heuristic naming + dependency direction | Cluster labels match MVC/hexagonal patterns |

### Surfacing

Each rule produces an `Insight(severity, title, description, affected_node_ids)`. Exposed via `GET /insights`. Frontend shows a badge count and an insights drawer with clickable links to affected nodes.

---

## 11. API Endpoints Summary

| Method | Path | Description |
|---|---|---|
| `POST` | `/analyze` | Trigger analysis of a target repo path |
| `GET` | `/analyze/status` | Poll analysis job status |
| `GET` | `/graph` | Get the full graph or top-level clusters (`?depth=1`) |
| `GET` | `/graph/{node_id}` | Get a single node with neighbours |
| `GET` | `/graph/{cluster_id}/children` | Expand a cluster's children |
| `GET` | `/search?q=...` | Fuzzy search nodes |
| `POST` | `/traces/ingest` | Ingest runtime trace spans |
| `GET` | `/insights` | Get detected patterns/anti-patterns |
| `WS` | `/ws/graph` | WebSocket for real-time graph updates |

---

## 12. Implementation Phases & Milestones

| Phase | Duration | Deliverable |
|---|---|---|
| **M1 — Skeleton & Python Parser** | 2 weeks | Project scaffolding, FastAPI server, Python `ast`-based parser, `networkx` graph store, basic `/graph` endpoint returning JSON. **Proof-of-life**: analyse a small Python repo and get JSON graph back. |
| **M2 — Frontend MVP** | 2 weeks | React + React Flow app displaying nodes/edges from `/graph`. Zoom, pan, click-to-detail with file path. Dagre layout. Search bar. |
| **M3 — Multi-language Parsers** | 2 weeks | Add `tree-sitter` parsers for JS/TS and Java. Parser registry. Cross-language import resolution (best-effort). |
| **M4 — LLM Summaries & Caching** | 1 week | Summariser, prompts, diskcache. Summaries appear in NodeDetail panel. Celery for background summarisation. |
| **M5 — Incremental & Scale** | 2 weeks | File watcher, delta re-parse, SQLite symbol cache, hierarchical lazy-loading API, Neo4j store option, WebSocket push for updates. |
| **M6 — Runtime Traces** | 1 week | OTLP ingestion endpoint, span-to-edge merging, heatmap visualisation on frontend. |
| **M7 — Pattern Detection** | 1 week | Rule engine, 5 built-in rules, insights API, insights drawer in frontend. |
| **M8 — IDE Extensions** | 2 weeks | VS Code extension (WebView + CodeLens), JetBrains plugin (JCEF + LineMarker). Bidirectional navigation. |
| **M9 — Polish & Release** | 1 week | Docker Compose setup, README with getting-started, CI (GitHub Actions for lint/test/build), `.env.example`, demo GIF/video. |

**Total estimated timeline: ~14 weeks** (solo developer, full-time).

---

## 13. Key Dependencies (`requirements.txt` preview)

```
fastapi>=0.110
uvicorn[standard]>=0.27
pydantic>=2.5
networkx>=3.2
tree-sitter>=0.21
pathspec>=0.12
watchfiles>=0.21
celery>=5.3
redis>=5.0
openai>=1.12
litellm>=1.20
tiktoken>=0.6
diskcache>=5.6
neo4j>=5.15
```

---

## 14. Open Design Decisions

1. **Graph DB default**: Start with NetworkX (zero infrastructure). Add Neo4j only if targeting repos > 10k files (deferred to M5). Could be made the default from the start if enterprise-scale is a priority.

2. **Offline LLM support**: The plan uses OpenAI via `litellm`. For fully offline operation, consider local models via Ollama (e.g., CodeLlama 13B) — at the cost of slower/lower-quality summaries. Could be added as an option in M4.

3. **Cross-language edge resolution**: Some repos mix languages (e.g., Python backend + TS frontend). The parser registry handles multi-language parsing, but statically resolving edges across language boundaries (e.g., a TS frontend calling a Python API endpoint) is hard. Heuristic URL/endpoint matching could be attempted in M3 or deferred to a future phase.

4. **Authentication / multi-user**: Current plan is single-user local tool. If hosted/multi-tenant support is needed, add auth layer (JWT) and per-user graph isolation.

---

*This plan serves as the project blueprint. Each milestone should begin with a more detailed task breakdown and acceptance criteria.*

