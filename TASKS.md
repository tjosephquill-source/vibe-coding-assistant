# VibeCodingAssistant — Task Breakdown & Timeline

> Granular task list organised by milestone, with estimated effort per task, dependencies, and acceptance criteria. Based on the architecture defined in [PLAN.md](PLAN.md).

**Start date:** Week 1  
**Estimated end date:** Week 14  
**Assumes:** 1 developer, full-time (~40 hrs/week)

---

## Legend

| Symbol | Meaning |
|--------|---------|
| 🟢 | No dependencies, can start immediately |
| 🔗 | Has prerequisite (listed in "Depends on" column) |
| ✅ | Acceptance criteria |

---

## M1 — Skeleton & Python Parser (Weeks 1–2)

### Week 1: Project scaffolding & core models

| # | Task | Est. | Depends on | Acceptance Criteria |
|---|------|------|------------|---------------------|
| 1.1 | 🟢 Initialise repo: `pyproject.toml`, `requirements.txt`, `.gitignore`, `README.md`, `.env.example` | 1h | — | ✅ `pip install -e .` succeeds; Git repo initialised |
| 1.2 | 🟢 Create backend directory structure (`backend/`, `api/`, `analysis/`, `graph/`, `llm/`, `traces/`, `patterns/`, `tests/`) with `__init__.py` files | 1h | — | ✅ All directories exist with empty init files |
| 1.3 | 🟢 Implement `backend/config.py` — Pydantic `BaseSettings` loading from `.env` (repo path, server host/port, LLM API key placeholder, graph backend toggle) | 1h | — | ✅ Settings load from env vars and `.env` file |
| 1.4 | 🔗 Implement `backend/graph/models.py` — Pydantic schemas: `NodeKind`, `EdgeKind`, `Node`, `Edge`, `GraphResponse` | 2h | 1.2 | ✅ Models serialise to/from JSON correctly; unit tests pass |
| 1.5 | 🔗 Implement `backend/graph/store.py` — Abstract `GraphStore` interface (`add_node`, `add_edge`, `get_node`, `get_neighbours`, `get_all`, `remove_node`, `remove_edge`, `clear`) | 1h | 1.4 | ✅ ABC defined with type hints |
| 1.6 | 🔗 Implement `backend/graph/networkx_store.py` — NetworkX implementation of `GraphStore` | 3h | 1.5 | ✅ All interface methods work; unit tests for add/get/remove/neighbours |
| 1.7 | 🔗 Implement `backend/main.py` — FastAPI app with CORS, lifespan event, mount store as app state | 1h | 1.3, 1.6 | ✅ `uvicorn backend.main:app` starts without errors |
| 1.8 | 🔗 Implement `backend/api/routes_graph.py` — `GET /graph` (full graph JSON), `GET /graph/{node_id}` (single node + neighbours) | 2h | 1.7 | ✅ Endpoints return correct JSON for a manually populated store |
| 1.9 | 🔗 Write `Makefile` with targets: `dev` (uvicorn reload), `test` (pytest), `lint` (ruff) | 1h | 1.7 | ✅ `make dev`, `make test`, `make lint` all work |

### Week 2: Python parser & analysis engine

| # | Task | Est. | Depends on | Acceptance Criteria |
|---|------|------|------------|---------------------|
| 1.10 | 🔗 Define intermediate types in `backend/analysis/parsers/base.py` — `SymbolDef`, `SymbolRef`, `ParseResult`, abstract `LanguageParser` class | 2h | 1.4 | ✅ Dataclasses/Pydantic models defined with all fields from PLAN |
| 1.11 | 🔗 Implement `backend/analysis/parsers/python_parser.py` — Use `ast.NodeVisitor` to extract: modules, classes, functions/methods, imports, function calls, inheritance, decorators | 8h | 1.10 | ✅ Correctly parses a sample Python file returning accurate `SymbolDef` and `SymbolRef` lists |
| 1.12 | 🔗 Implement `backend/analysis/parsers/registry.py` — Map file extensions to parser instances (`.py` → `PythonParser`) | 1h | 1.11 | ✅ `registry.get_parser(".py")` returns `PythonParser` instance |
| 1.13 | 🔗 Implement `backend/analysis/engine.py` — `AnalysisEngine`: walk target repo (respect `.gitignore` via `pathspec`), dispatch files to parsers, collect all `SymbolDef`/`SymbolRef` | 4h | 1.12 | ✅ Engine walks a test repo and collects symbols from all `.py` files |
| 1.14 | 🔗 Implement name resolution in `engine.py` — Build symbol table (`qualified_name → SymbolDef`), resolve `SymbolRef` targets using import paths | 4h | 1.13 | ✅ Cross-file imports correctly resolved; unresolved refs marked as `tentative` |
| 1.15 | 🔗 Implement `backend/analysis/graph_builder.py` — Convert resolved symbols into `Node`/`Edge` objects, compute `fan_in`/`fan_out`, assign `cluster` by package path, populate `GraphStore` | 4h | 1.14, 1.6 | ✅ A small multi-file Python project produces a correct graph with nodes and edges |
| 1.16 | 🔗 Implement `backend/api/routes_analysis.py` — `POST /analyze` (accepts `{repo_path}`, triggers analysis, returns job ID), `GET /analyze/status` (returns progress/completion) | 3h | 1.15, 1.8 | ✅ Can POST a repo path, poll status, then GET /graph returns populated data |
| 1.17 | 🔗 Create test fixtures in `backend/tests/fixtures/` — Small multi-file Python project with classes, imports, inheritance, function calls | 1h | — | ✅ Fixture files exist and cover key relationship types |
| 1.18 | 🔗 Write unit tests: `test_parsers.py` (Python parser), `test_graph_builder.py` (end-to-end fixture → graph) | 4h | 1.17, 1.15 | ✅ All tests pass; ≥80% coverage on parser and graph builder |

**M1 Total: ~41 hours**  
**M1 Deliverable:** POST a Python repo path → GET a JSON graph of its architecture.

---

## M2 — Frontend MVP (Weeks 3–4)

### Week 3: React project setup & graph rendering

| # | Task | Est. | Depends on | Acceptance Criteria |
|---|------|------|------------|---------------------|
| 2.1 | 🟢 Scaffold frontend: `npm create vite@latest frontend -- --template react-ts`, install deps (`reactflow`, `@dagrejs/dagre`, `tailwindcss`, `axios`, `fuse.js`) | 1h | — | ✅ `npm run dev` serves app at localhost:5173 |
| 2.2 | 🔗 Define TypeScript types in `frontend/src/types/graph.ts` — Mirror backend `Node`, `Edge`, `NodeKind`, `EdgeKind` | 1h | 2.1 | ✅ Types compile without errors |
| 2.3 | 🔗 Implement `frontend/src/api/client.ts` — Axios client with base URL config, `fetchGraph()`, `analyzeRepo()`, `getAnalysisStatus()` | 2h | 2.2 | ✅ Functions call correct endpoints and return typed responses |
| 2.4 | 🔗 Implement `frontend/src/layouts/algorithms.ts` — Dagre layout function: takes nodes/edges → returns positioned nodes | 3h | 2.2 | ✅ Layout produces non-overlapping positions for a test graph |
| 2.5 | 🔗 Implement `frontend/src/utils/colors.ts` — Color palette and icon mapping per `NodeKind` and `EdgeKind` | 1h | 2.2 | ✅ Every kind has a distinct colour and edge style |
| 2.6 | 🔗 Implement `frontend/src/components/GraphCanvas.tsx` — React Flow canvas with custom node rendering (icon + label + kind badge), edge styles (solid/dashed/dotted by kind), Dagre layout on load | 6h | 2.4, 2.5 | ✅ Renders a graph fetched from backend with zoom/pan working |
| 2.7 | 🔗 Implement `frontend/src/hooks/useGraph.ts` — Custom hook: fetch graph on mount, transform backend data → React Flow nodes/edges, manage loading/error state | 3h | 2.3, 2.6 | ✅ Hook provides `nodes`, `edges`, `isLoading`, `error` |

### Week 4: Interaction, search, detail panel

| # | Task | Est. | Depends on | Acceptance Criteria |
|---|------|------|------------|---------------------|
| 2.8 | 🔗 Implement `frontend/src/components/NodeDetail.tsx` — Side panel shown on node click: name, kind, qualified name, file path (clickable link), line range, metrics table, summary (placeholder), source code preview area | 4h | 2.6 | ✅ Clicking a node opens panel with correct data; clicking away closes it |
| 2.9 | 🔗 Implement `frontend/src/components/SearchBar.tsx` — Fuse.js fuzzy search over node names, dropdown results, click to focus/highlight node on canvas | 3h | 2.7 | ✅ Typing a partial name shows matching nodes; selecting one centres the view |
| 2.10 | 🔗 Implement `frontend/src/hooks/useSearch.ts` — Search state management, Fuse.js instance initialisation | 2h | 2.9 | ✅ Hook integrates with useGraph data |
| 2.11 | 🔗 Implement `frontend/src/components/MiniMap.tsx` — React Flow MiniMap with node colours matching kind | 1h | 2.6 | ✅ Minimap visible in corner, colours match node types |
| 2.12 | 🔗 Implement `frontend/src/components/FilterPanel.tsx` — Checkboxes to toggle node kinds and edge kinds visibility | 3h | 2.7 | ✅ Unchecking "imports" hides import edges; unchecking "function" hides function nodes |
| 2.13 | 🔗 Implement `frontend/src/App.tsx` — Compose all components: GraphCanvas, SearchBar, FilterPanel, NodeDetail, MiniMap. Responsive layout with TailwindCSS | 3h | 2.8–2.12 | ✅ Full UI renders with all components working together |
| 2.14 | 🔗 Add Vite proxy config (`vite.config.ts`) to proxy `/api` to backend; add CORS to backend for dev | 1h | 2.13 | ✅ Frontend can call backend without CORS issues in dev |
| 2.15 | 🔗 End-to-end smoke test: run backend + frontend, analyse a Python project, see graph in browser | 2h | 2.14, M1 | ✅ Full flow works: analyse → visualise → click node → see details |

**M2 Total: ~36 hours**  
**M2 Deliverable:** Interactive graph visualisation of a Python codebase in the browser.

---

## M3 — Multi-language Parsers (Weeks 5–6)

### Week 5: Tree-sitter infrastructure & JS/TS parser

| # | Task | Est. | Depends on | Acceptance Criteria |
|---|------|------|------------|---------------------|
| 3.1 | 🔗 Install `tree-sitter` and language grammars (`tree-sitter-javascript`, `tree-sitter-typescript`, `tree-sitter-java`); configure grammar loading in a shared utility | 3h | M1 | ✅ Grammars load and can parse sample files without errors |
| 3.2 | 🔗 Implement `backend/analysis/parsers/js_ts_parser.py` — Tree-sitter queries for: `import`/`require`, `export`, `class`, `function`/arrow function, method calls, `extends`/`implements` | 8h | 3.1, 1.10 | ✅ Correctly extracts symbols and references from a sample TS project |
| 3.3 | 🔗 Add JS/TS test fixtures in `backend/tests/fixtures/` — Small TS project with classes, imports, React components | 1h | — | ✅ Fixtures cover ES modules, CommonJS, class inheritance |
| 3.4 | 🔗 Write unit tests for JS/TS parser | 3h | 3.2, 3.3 | ✅ All parser tests pass for JS/TS fixtures |
| 3.5 | 🔗 Register `.js`, `.jsx`, `.ts`, `.tsx` extensions in `registry.py` | 0.5h | 3.2 | ✅ Registry returns JsTsParser for all JS/TS extensions |

### Week 6: Java parser & cross-language resolution

| # | Task | Est. | Depends on | Acceptance Criteria |
|---|------|------|------------|---------------------|
| 3.6 | 🔗 Implement `backend/analysis/parsers/java_parser.py` — Tree-sitter queries for: `import`, `class`, `interface`, `method`, annotations, `extends`/`implements`, constructor calls | 6h | 3.1, 1.10 | ✅ Correctly extracts symbols from a sample Java project |
| 3.7 | 🔗 Add Java test fixtures and unit tests | 3h | 3.6 | ✅ All parser tests pass for Java fixtures |
| 3.8 | 🔗 Register `.java` in `registry.py` | 0.5h | 3.6 | ✅ Registry returns JavaParser for `.java` |
| 3.9 | 🔗 Extend `engine.py` name resolution for multi-language — Handle JS/TS module resolution (`./relative`, `package`, `@scope/package`), Java package imports | 6h | 3.5, 3.8 | ✅ Cross-file references resolved within each language; unresolved marked tentative |
| 3.10 | 🔗 Update frontend `colors.ts` and node rendering for new languages (language badge on nodes) | 2h | 3.9, M2 | ✅ JS/TS and Java nodes display with distinct language indicators |
| 3.11 | 🔗 Integration test: analyse a polyglot repo (Python + TS) end-to-end | 2h | 3.9, 3.10 | ✅ Both languages appear in the graph with correct intra-language edges |

**M3 Total: ~35 hours**  
**M3 Deliverable:** Tool analyses Python, JavaScript/TypeScript, and Java codebases.

---

## M4 — LLM Summaries & Caching (Week 7)

| # | Task | Est. | Depends on | Acceptance Criteria |
|---|------|------|------------|---------------------|
| 4.1 | 🔗 Install `openai`, `litellm`, `tiktoken`, `diskcache`; add to `requirements.txt` | 0.5h | M1 | ✅ All packages install cleanly |
| 4.2 | 🔗 Implement `backend/llm/prompts.py` — Prompt templates for node summarisation and edge summarisation, with `{kind}`, `{language}`, `{source_code}` placeholders | 2h | 4.1 | ✅ Templates render correctly with test data |
| 4.3 | 🔗 Implement `backend/llm/cache.py` — `SummaryCache` class using `diskcache`: `get(key)`, `set(key, value, ttl)`, key = `sha256(code + prompt_version)` | 2h | 4.1 | ✅ Cache stores and retrieves; TTL expiry works |
| 4.4 | 🔗 Implement `backend/llm/summariser.py` — `Summariser` class: accepts `Node`/`Edge`, reads source code from file, truncates to 3k tokens via `tiktoken`, calls LLM via `litellm`, caches result; includes docstring fallback | 6h | 4.2, 4.3 | ✅ Generates summaries for test nodes; uses cache on repeat calls; falls back to docstring when LLM unavailable |
| 4.5 | 🔗 Install `celery`, `redis`; add to `requirements.txt`; implement `backend/tasks.py` — Celery app config, `summarise_batch` task (takes list of node IDs, calls summariser for each) | 4h | 4.4 | ✅ Celery worker picks up tasks; summaries written to graph store |
| 4.6 | 🔗 Add `POST /analyze` flow to trigger summarisation tasks after graph is built (fire-and-forget batch tasks for all nodes without summaries) | 2h | 4.5, 1.16 | ✅ After analysis completes, summaries appear on nodes asynchronously |
| 4.7 | 🔗 Update `NodeDetail.tsx` to display LLM summary (with loading spinner while pending) | 2h | 4.6, M2 | ✅ Summary appears in detail panel; shows "Generating…" while pending |
| 4.8 | 🔗 Write unit tests for summariser (mock LLM calls) and cache | 3h | 4.4, 4.3 | ✅ Tests pass with mocked LLM; cache hit/miss scenarios covered |
| 4.9 | 🔗 Add `docker-compose.yml` with Redis service for local Celery development | 1h | 4.5 | ✅ `docker-compose up redis` starts Redis; Celery connects |

**M4 Total: ~22.5 hours**  
**M4 Deliverable:** Every node and edge gets an auto-generated natural language summary.

---

## M5 — Incremental Analysis & Scale (Weeks 8–9)

### Week 8: Incremental parsing & caching

| # | Task | Est. | Depends on | Acceptance Criteria |
|---|------|------|------------|---------------------|
| 5.1 | 🔗 Implement file-hash tracking in `engine.py` — SQLite DB storing `{file_path, content_hash, last_parsed}` via `sqlite3` | 3h | M1 | ✅ DB created on first run; hashes stored and queryable |
| 5.2 | 🔗 Implement `backend/analysis/incremental.py` — `IncrementalEngine`: compare file hashes on re-analysis, re-parse only changed/new files, remove deleted files' symbols, patch graph store | 6h | 5.1, 1.15 | ✅ Modifying one file re-parses only that file; graph updates correctly |
| 5.3 | 🔗 Add file watcher using `watchfiles` — Watch target repo, trigger incremental re-analysis on file changes (debounced 1s) | 3h | 5.2 | ✅ Editing a file in the target repo triggers graph update within 2s |
| 5.4 | 🔗 Persist parsed symbol data to SQLite sidecar — On restart, load symbols from DB instead of full re-parse | 3h | 5.1 | ✅ Restarting the server with unchanged repo skips parsing entirely |

### Week 9: Hierarchical loading, WebSocket, Neo4j

| # | Task | Est. | Depends on | Acceptance Criteria |
|---|------|------|------------|---------------------|
| 5.5 | 🔗 Extend `GET /graph` with `?depth=N` parameter — depth=1 returns only package-level cluster nodes, depth=2 expands one level, etc. | 3h | 1.8, 1.15 | ✅ `/graph?depth=1` returns < 50 nodes for a large repo |
| 5.6 | 🔗 Add `GET /graph/{cluster_id}/children` — Returns child nodes/edges within a cluster | 2h | 5.5 | ✅ Expanding a cluster returns its immediate children |
| 5.7 | 🔗 Implement `backend/api/ws_graph.py` — WebSocket endpoint `/ws/graph` that pushes graph diffs (added/removed nodes/edges) when incremental analysis runs | 4h | 5.3 | ✅ Connected WebSocket client receives update messages on file change |
| 5.8 | 🔗 Update frontend `useGraph.ts` to connect to WebSocket, apply incremental updates to React Flow state | 4h | 5.7, M2 | ✅ Graph updates in real-time when a file changes in the target repo |
| 5.9 | 🔗 Update frontend `GraphCanvas.tsx` for cluster expand/collapse — Double-click a cluster node to fetch children and expand in place | 4h | 5.6, 2.6 | ✅ Clusters expand/collapse smoothly with layout re-computation |
| 5.10 | 🔗 Implement `backend/graph/neo4j_store.py` — Neo4j implementation of `GraphStore` interface using `neo4j` async driver | 6h | 1.5 | ✅ All `GraphStore` interface tests pass against Neo4j (Testcontainers or local instance) |
| 5.11 | 🔗 Add Neo4j service to `docker-compose.yml`; config toggle in `config.py` (`GRAPH_BACKEND=networkx|neo4j`) | 1h | 5.10, 4.9 | ✅ Switching config to neo4j uses Neo4j store transparently |

**M5 Total: ~39 hours**  
**M5 Deliverable:** Live-updating graph with incremental analysis, hierarchical navigation, and optional Neo4j backend.

---

## M6 — Runtime Traces (Week 10)

| # | Task | Est. | Depends on | Acceptance Criteria |
|---|------|------|------------|---------------------|
| 6.1 | 🔗 Define trace ingestion schemas in `backend/traces/ingest.py` — Pydantic models for OTLP span batch and simplified `{caller, callee, timestamp, duration_ms, status}` format | 2h | M1 | ✅ Both formats validate correctly |
| 6.2 | 🔗 Implement `backend/api/routes_traces.py` — `POST /traces/ingest` accepting both formats, stores raw spans in a buffer (in-memory deque, max 100k) | 2h | 6.1 | ✅ Endpoint accepts and stores spans; returns 201 |
| 6.3 | 🔗 Implement `backend/traces/merge.py` — `TraceMerger`: match `operation_name` to node `qualified_name` (exact then fuzzy via `difflib.SequenceMatcher`), aggregate per-edge (`call_count`, `p50_latency`, `p99_latency`, `error_rate`), update `node.runtime` and `edge.weight` | 6h | 6.2, 1.6 | ✅ After ingesting test spans, graph nodes/edges have correct runtime stats |
| 6.4 | 🔗 Add `@trace` Python decorator helper in `backend/traces/` — Lightweight decorator that records caller/callee/duration and POSTs to `/traces/ingest` | 2h | 6.2 | ✅ Decorated functions send trace data to local server |
| 6.5 | 🔗 Update frontend: heatmap colouring on nodes/edges based on `runtime.call_count` and `runtime.avg_latency_ms` — Colour scale from blue (cold) to red (hot) | 3h | 6.3, M2 | ✅ Nodes with high call counts appear red; hovering shows stats tooltip |
| 6.6 | 🔗 Update `NodeDetail.tsx` to show runtime stats section (call count, latency percentiles, error rate) when available | 2h | 6.5 | ✅ Runtime section appears only for nodes with trace data |
| 6.7 | 🔗 Add animated edges for hot paths (top 10% by call count) | 2h | 6.5 | ✅ High-traffic edges have visible animation |
| 6.8 | 🔗 Write tests for trace ingestion and merge logic with mock spans | 3h | 6.3 | ✅ Tests cover exact match, fuzzy match, aggregation maths |

**M6 Total: ~22 hours**  
**M6 Deliverable:** Runtime behaviour overlaid on the static architecture graph.

---

## M7 — Pattern Detection (Week 11)

| # | Task | Est. | Depends on | Acceptance Criteria |
|---|------|------|------------|---------------------|
| 7.1 | 🔗 Define `Insight` model in `backend/patterns/detector.py` — `Insight(severity: str, title: str, description: str, affected_node_ids: list[str], rule_name: str)` | 1h | M1 | ✅ Model serialises correctly |
| 7.2 | 🔗 Implement `PatternDetector` class — Runs all registered rules against the graph store, collects insights | 2h | 7.1 | ✅ Detector runs rules and returns list of insights |
| 7.3 | 🔗 Implement rule: **Circular dependency detection** — Tarjan's SCC on IMPORTS subgraph via `networkx.strongly_connected_components`; report cycles > 1 node | 2h | 7.2 | ✅ Detects known circular import in test fixture |
| 7.4 | 🔗 Implement rule: **God class detection** — Flag nodes with `loc > 500` AND `fan_out > 20` | 1h | 7.2 | ✅ Flags a 600-line class with 25 dependencies in test fixture |
| 7.5 | 🔗 Implement rule: **Orphan module detection** — Nodes with `fan_in == 0` AND `fan_out == 0`, excluding nodes tagged `entrypoint` | 1h | 7.2 | ✅ Finds orphan module in test fixture; ignores tagged entrypoints |
| 7.6 | 🔗 Implement rule: **Layer violation detection** — Accept user-defined layer config (list of layer names in order); detect edges that skip or reverse layer order | 3h | 7.2 | ✅ Detects controller → repository direct call when layers defined as controller → service → repository |
| 7.7 | 🔗 Implement rule: **Architecture pattern inference** — Heuristic naming + dependency direction analysis to label clusters as MVC, layered, hexagonal | 3h | 7.2 | ✅ Labels a standard Django project as MVC-like |
| 7.8 | 🔗 Implement `GET /insights` endpoint — Returns all insights, filterable by severity and rule | 2h | 7.2 | ✅ Endpoint returns JSON array of insights |
| 7.9 | 🔗 Build frontend insights drawer — Badge count on toolbar, click to open drawer listing insights with severity icons, clickable affected nodes | 4h | 7.8, M2 | ✅ Clicking an insight highlights affected nodes on the graph |
| 7.10 | 🔗 Write tests for all 5 detection rules with purpose-built fixtures | 4h | 7.3–7.7 | ✅ Each rule has ≥2 test cases (positive and negative) |

**M7 Total: ~23 hours**  
**M7 Deliverable:** Automatic architectural insight detection surfaced in the UI.

---

## M8 — IDE Extensions (Weeks 12–13)

### Week 12: VS Code extension

| # | Task | Est. | Depends on | Acceptance Criteria |
|---|------|------|------------|---------------------|
| 8.1 | 🟢 Scaffold VS Code extension in `extensions/vscode/` using `yo code` generator (TypeScript) | 1h | — | ✅ Extension compiles and loads in VS Code Extension Development Host |
| 8.2 | 🔗 Implement command `VibeCodingAssistant: Show Graph` — Opens a WebView panel loading the React frontend URL (configurable, default `http://localhost:5173`) | 3h | 8.1 | ✅ Command opens panel with embedded graph UI |
| 8.3 | 🔗 Implement WebView ↔ extension messaging — Define message protocol: `{type: "navigateToFile", filePath, line}` from WebView, `{type: "focusNode", nodeId}` from extension | 3h | 8.2 | ✅ Clicking a node in WebView opens the file at the correct line in VS Code |
| 8.4 | 🔗 Update frontend to detect WebView context and send `navigateToFile` messages via `vscode.postMessage()` instead of URI links | 2h | 8.3, M2 | ✅ File navigation works both in browser (URI) and in VS Code WebView (message) |
| 8.5 | 🔗 Implement `CodeLensProvider` — Show "🔍 View in Graph" above each class/function definition; clicking sends `focusNode` message to WebView | 4h | 8.3 | ✅ CodeLens appears above classes and functions; clicking centres graph on that node |
| 8.6 | 🔗 Package extension: `vsce package`, add install instructions to README | 1h | 8.5 | ✅ `.vsix` file installs and works |

### Week 13: JetBrains plugin

| # | Task | Est. | Depends on | Acceptance Criteria |
|---|------|------|------------|---------------------|
| 8.7 | 🟢 Scaffold JetBrains plugin in `extensions/jetbrains/` using IntelliJ Platform Plugin Template (Kotlin, Gradle) | 2h | — | ✅ Plugin compiles and loads in a sandbox IDE |
| 8.8 | 🔗 Implement tool window with JCEF browser panel — Loads React frontend URL | 4h | 8.7 | ✅ Tool window shows graph UI inside JetBrains IDE |
| 8.9 | 🔗 Implement JCEF ↔ plugin messaging bridge — JS `window.cefQuery` for `navigateToFile`; Kotlin `JBCefJSQuery` for `focusNode` | 4h | 8.8 | ✅ Bidirectional navigation works |
| 8.10 | 🔗 Update frontend to detect JCEF context and use `cefQuery` for file navigation | 2h | 8.9, 8.4 | ✅ Navigation works in JetBrains, VS Code, and browser contexts |
| 8.11 | 🔗 Implement `LineMarkerProvider` — Gutter icon "🔍" next to class/function definitions; click sends `focusNode` to JCEF panel | 4h | 8.9 | ✅ Gutter icons appear; clicking navigates graph |
| 8.12 | 🔗 Build and package plugin: `./gradlew buildPlugin`, add install instructions | 1h | 8.11 | ✅ Plugin `.zip` installs in JetBrains IDE |

**M8 Total: ~31 hours**  
**M8 Deliverable:** Working VS Code extension and JetBrains plugin with bidirectional code ↔ graph navigation.

---

## M9 — Polish & Release (Week 14)

| # | Task | Est. | Depends on | Acceptance Criteria |
|---|------|------|------------|---------------------|
| 9.1 | 🔗 Complete `docker-compose.yml` — Services: backend (FastAPI), frontend (Vite build → nginx), Redis, Neo4j (optional). Health checks, volume mounts | 3h | All milestones | ✅ `docker-compose up` starts entire stack; app accessible at localhost:3000 |
| 9.2 | 🔗 Write comprehensive `README.md` — Project overview, quick start (local + Docker), configuration guide, screenshots, architecture diagram, contributing guide | 4h | 9.1 | ✅ A new developer can get the tool running by following the README |
| 9.3 | 🔗 Populate `.env.example` with all config vars and comments | 0.5h | 9.1 | ✅ All settings documented |
| 9.4 | 🔗 Set up GitHub Actions CI — Lint (ruff), test (pytest), type-check (mypy), frontend build, extension build | 4h | All milestones | ✅ CI passes on a clean checkout |
| 9.5 | 🔗 Add `mypy` type checking to backend; fix any type errors | 3h | All backend tasks | ✅ `mypy backend/` passes with no errors |
| 9.6 | 🔗 Frontend production build optimisation — Code splitting, lazy loading for NodeDetail/FilterPanel, Vite build config | 2h | M2 | ✅ Production build < 500KB gzipped; Lighthouse performance > 90 |
| 9.7 | 🔗 Record demo GIF/video — Show: analyse repo → explore graph → click node → see summary → detect pattern → navigate to code | 2h | 9.1 | ✅ Demo clearly shows full workflow; linked in README |
| 9.8 | 🔗 Final integration test — Clone a real open-source Python project (e.g., Flask), run full pipeline, verify graph accuracy | 3h | 9.1 | ✅ Flask codebase produces a navigable, accurate architecture graph |
| 9.9 | 🔗 Write `CONTRIBUTING.md` — Development setup, code style, PR process, architecture overview for contributors | 2h | 9.2 | ✅ Document exists with all sections |
| 9.10 | 🔗 Create GitHub release with changelog | 1h | 9.4 | ✅ Tagged release with assets (Docker image tags, extension packages) |

**M9 Total: ~24.5 hours**  
**M9 Deliverable:** Production-ready, documented, CI-tested release.

---

## Summary: Effort by Milestone

| Milestone | Weeks | Est. Hours | Cumulative |
|-----------|-------|------------|------------|
| M1 — Skeleton & Python Parser | 1–2 | 41h | 41h |
| M2 — Frontend MVP | 3–4 | 36h | 77h |
| M3 — Multi-language Parsers | 5–6 | 35h | 112h |
| M4 — LLM Summaries & Caching | 7 | 22.5h | 134.5h |
| M5 — Incremental & Scale | 8–9 | 39h | 173.5h |
| M6 — Runtime Traces | 10 | 22h | 195.5h |
| M7 — Pattern Detection | 11 | 23h | 218.5h |
| M8 — IDE Extensions | 12–13 | 31h | 249.5h |
| M9 — Polish & Release | 14 | 24.5h | 274h |

**Grand total: ~274 hours across 14 weeks (~19.6 hrs/week average)**

---

## Critical Path

The longest dependency chain that determines the minimum project duration:

```
1.2 → 1.4 → 1.5 → 1.6 → 1.7 → 1.8 → 1.16 (M1 backend complete)
                                          ↓
                                    2.1 → 2.6 → 2.7 → 2.13 → 2.15 (M2 frontend complete)
                                                                  ↓
                                                            5.7 → 5.8 (M5 real-time updates)
                                                                  ↓
                                                            7.8 → 7.9 (M7 insights UI)
                                                                  ↓
                                                            9.1 → 9.8 (M9 release)
```

M3, M4, M6, and M8 can partially overlap with other milestones where dependencies allow. In particular:
- **M3** (multi-lang parsers) and **M4** (LLM) can start in parallel once M1 is done.
- **M8** (IDE extensions) can start as soon as M2 is done (the frontend is the only dependency).

---

## Risk Register

| Risk | Impact | Mitigation |
|------|--------|------------|
| Tree-sitter grammar loading issues on macOS/ARM | Blocks M3 | Pre-test grammar compilation in M1; have pure-Python fallback parsers |
| LLM API rate limits / downtime | Degrades M4 | Docstring fallback; local Ollama as backup; aggressive caching |
| Neo4j adds operational complexity | Slows M5 adoption | Keep NetworkX as default; Neo4j is opt-in |
| React Flow performance with 10k+ nodes | Degrades M5 UX | Hierarchical loading limits visible nodes to ~500; cluster collapse |
| Cross-language edge resolution inaccurate | Reduces M3 usefulness | Mark cross-language edges as "inferred" with low confidence; allow manual correction |
| Celery/Redis adds deployment complexity | Slows M4/M9 | For MVP, run summarisation synchronously in-process; Celery is optional |

---

*This task breakdown is a living document. Update task statuses and estimates as work progresses. Each task should be converted to an issue/ticket in your project tracker.*

