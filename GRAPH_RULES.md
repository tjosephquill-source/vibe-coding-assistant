# Graph Rules

---

## Part 1 — Node Inclusion

Rules that decide whether a symbol becomes a node in the graph, and which kind of node it becomes.

### 1. File Eligibility
- **Included** file extensions: `.py`, `.js`, `.jsx`, `.ts`, `.tsx`, `.html`, `.htm`
- **Excluded** directories (never walked): `__pycache__`, `node_modules`, `.git`, `dist`, `build`,
  `.next`, `.nuxt`, `coverage`, `.nyc_output`, `vendor`, `bower_components`, `.tox`,
  `.mypy_cache`, `.pytest_cache`, `venv`, `.venv`, `env`, `.env`, `.idea`, `.vscode`,
  `.eggs`, `egg-info`, `.bundle`, `.cache`, `.parcel-cache`, `target`, `out`, `bin`,
  `obj`, `lib`, `.gradle`, `.mvn`, `.terraform`, `.serverless`
- Any path component that starts with `.` is also skipped

### 2. Symbol Extraction
- **Python** — full AST parse; emits a node for every `class` and every method (`def` / `async def`) defined inside a class
  - Top-level (module-level) functions are **not** included
  - Nested classes are included; their methods are attributed to the inner class
- **JavaScript / TypeScript** — regex-based parse; emits a node for every `class … {}` and every method inside one
  - Files that yield zero classes and zero methods are silently skipped
- **HTML** — inline `<script>` blocks are extracted and treated as JS; all other HTML markup is ignored

### 3. Node Kind Assignment (raw graph)
| Kind | Criteria |
|---|---|
| `class` | Any class definition found in a source file |
| `method` | Any method (function inside a class) |

### 4. Edge Kind Assignment
| Kind | When created |
|---|---|
| `contains` | Parent class → child method (structural ownership) |
| `inherits` | Class A `extends` / `(Base)` Class B, where B is in the graph |
| `instantiates` | `new ClassName()` or a call that matches a known class name, within a class context |
| `calls` | Method A calls Method B, both in the same class hierarchy |
| `uses_type` | A type annotation or parameter annotation references a known class |
| `imports` | Class A imports Class B but no stronger relationship is detected |

### 5. Abstract / Heuristic Grouping (abstract graph view)
When the abstract view is requested, all `class` nodes are first classified by naming and path conventions, then collapsed into group nodes.

#### 5.1 Classification priority (first match wins)
1. **Exception / Error** — name ends with `Error`, `Exception`, `Warning`, `Fault`, `Exc`, `Failure`, `Problem`, `Issue`; or starts with `Invalid`; or inherits from a known error base; or lives in `exceptions.py`, `errors.py`, `warnings.py`
2. **Event / Signal** — name ends with `Event`, `Signal`, `Command`, `Message`, `Notification`, `Trigger`, `Hook`, `Listener`, `Observer`, `Subscriber`, `Publisher`, `Emitter`, `Dispatcher`; or lives in `events.py`, `signals.py`, `commands.py`, etc.
3. **Test** — name starts / ends with `Test` / `Tests` / `Spec` / `Suite`; or inherits `TestCase`; or lives in a `test_*.py`, `*_test.py`, `tests.py`, `.test.js`, `.spec.ts`, or any `tests/`, `test/`, `__tests__/` directory
4. **Model / Schema** — name ends with `Model`, `Schema`, `Entity`, `Record`, `DTO`, `Document`, `Row`, `Aggregate`, `Payload`, `Request`, `Response`, `Serializer`; or inherits `BaseModel`, `DeclarativeBase`, `TypedDict`, etc.; or lives in `models.py`, `schemas.py`, `entities.py`, `types.py`
5. **Config / Settings** — name ends with `Config`, `Settings`, `Options`, `Constants`, `Configuration`, `Env`, `Params`, `Parameters`, `Flags`, `Feature`; or contains `config` / `settings` anywhere in the name; or lives in `config.py`, `settings.py`, `constants.py`, `params.py`
6. **Service / Handler** — name ends with `Service`, `Handler`, `Manager`, `Controller`, `View`, `Router`, `Processor`, `Worker`, `Repository`, `Repo`, `Gateway`, `Client`, `Adapter`, `Facade`, `Coordinator`, `Orchestrator`, `Task`, `Job`, `Action`, `UseCase`, `Builder`, `Pipeline`, `Component`; or lives in `services.py`, `handlers.py`, `controllers.py`, `repositories.py`, etc.
7. **Utility / Helper** — name ends with `Helper`, `Utils`, `Util`, `Mixin`, `Utilities`, `Tools`, `Common`, `Decorator`, `Middleware`, `Guard`, `Filter`, `Validator`, `Formatter`, `Parser`, `Converter`, `Registry`, `Cache`, `Pool`, `Proxy`, `Wrapper`, `Singleton`; or starts with `Base`, `Abstract`, or `Mixin`; or lives in `utils.py`, `helpers.py`, `mixins.py`, `base.py`, etc.
8. **File group** (fallback) — unclassified classes that share a source file with at least one other unclassified class are grouped by filename stem

#### 5.2 Sub-grouping within a category
- Nodes of the same category are first bucketed by source file
- Buckets smaller than `min_group_size = 2` are merged into the largest bucket of the same category
- Each resulting bucket becomes one group node

#### 5.3 Community detection (higher abstraction levels)
- Connected components with `≤ 2` bridge edges and `≥ 3` members are collapsed into a `island_chain` node
- Oversized components (`> 100` nodes) are subdivided by seed expansion before collapsing
- Communities are ranked by modularity (internal / total edges); non-overlapping communities are selected greedily
- `method` nodes belonging to a collapsed class are also collapsed into the same island chain

### 6. Node Exclusion Rules
- Duplicate nodes (same `id`) are silently dropped — only the first occurrence is kept
- Duplicate edges (same source, target, and kind triple) are silently dropped
- Self-loop edges (source == target after remapping) are discarded
- `contains` edges that cross a collapse boundary are discarded

---

## Part 2 — Node Positioning and Spacing

Rules that determine where each node is placed in the overview viewport.

### 1. Layout Pipeline
Every time the graph is rendered the following stages run in order:

1. **Memory position cache** — if positions for this graph were computed previously in the same session, they are reapplied instantly (no simulation)
2. **Disk position cache** — if a valid on-disk cache exists (keyed by a fingerprint of all source files), positions are loaded and applied
3. **Seed positioning** — initial coordinates are assigned before the force simulation starts
4. **Force simulation** — D3 forces settle the layout
5. **Grid snap** — after the simulation ends, each node is snapped to the nearest unoccupied grid cell
6. **Fit to viewport** — the camera is adjusted so all nodes are visible

### 2. Seed Positioning
- **Connected components** are laid out first
  - Components with `> 1` node are arranged in a square grid with `COMPONENT_GAP_X = 520 px` horizontal spacing and `COMPONENT_GAP_Y = 420 px` vertical spacing, centred in the viewport
  - Within each component, nodes are sorted by degree (highest first)
  - The top 20 % of high-degree nodes are placed in a small ring of radius `40 px` around the component centre
  - Remaining nodes are placed in concentric rings of radius `90 + ring × 70 px`, up to 8 nodes per ring
- **Isolated nodes** (single-node components) are placed in a separate grid row below the connected graph
  - Vertical offset from the bottom of the connected area: `ISOLATED_ROW_Y_OFFSET = 420 px`
  - Column gap: `ISOLATED_COL_GAP = 280 px` (reduced to `300 px` when there are more than 12 isolated nodes)
  - Row gap follows `GRID_SNAP_ROW_GAP` (see §5)
  - Number of columns: `ceil(sqrt(n × 1.4))`, centred horizontally

### 3. Force Simulation
The simulation runs only on connected nodes (isolated nodes are pre-positioned and excluded from forces).

| Force | Parameters |
|---|---|
| `alphaDecay` | `0.03` (slow cool-down for a well-settled layout) |
| `velocityDecay` | `0.35` |
| **Link** | Distance by edge kind (see §3.1); strength `0.35` for `contains`, `0.2` for all others; `3` iterations per tick |
| **Charge** (repulsion) | Strength by node kind (see §3.2) |
| **Centre** | `width / 2`, `height / 2`, keeps the graph in view during simulation |
| **Collision** | Radius = `√(w² + h²) / 2 + COLLISION_PAD` where `COLLISION_PAD = 30 px` |
| **Isolated pull** | Custom force pulls isolated nodes toward their `_isolatedTargetY` row; strength `ISOLATED_FORCE_STRENGTH = 0.18` |
| **Weak X/Y centre** | `forceX` and `forceY` toward viewport centre, strength `0.025` each |

The simulation is given a maximum of `4 000 ms`; if it has not converged by then it is stopped and snapped.

#### 3.1 Link distances (preferred edge length)
| Edge kind | Distance |
|---|---|
| `contains` | 130 px |
| `inherits` | 260 px |
| `instantiates` | 260 px |
| `uses_type` | 240 px |
| `calls` | 230 px |
| `imports` | 210 px |

#### 3.3 Edge-node avoidance force
Edges must not visually pass through unrelated nodes.  A custom force detects when a node **W** lies close to the interior of an edge **U → V** and pushes W away perpendicular to that edge.

- **Trigger condition**: the orthogonal projection of W onto segment U→V has a parameter `t ∈ (0.1, 0.9)` — i.e. the nearest point on the edge is in the interior, not near either endpoint
- **Clearance required**: `√(nodeW² + nodeH²) / 2 + COLLISION_PAD` (same radius used by the collision force)
- **Impulse**: `strength × alpha × (minDist − dist) / minDist` — fades naturally as the simulation cools; stronger push the closer W is to the line
- **Degenerate case**: if W lies exactly on the line, it is pushed in the direction of the edge's perpendicular instead of a zero-length vector
- **Strength**: `0.7` (tunable constant)
- **Performance guard**: only applied when the simulated node count is **≤ 150** (O(E × N) per tick is acceptable up to that threshold)
- **Alpha threshold**: no-ops when `alpha < 0.005` since tiny perturbations near equilibrium are not worth the CPU cost

#### 3.2 Node repulsion charges
| Node kind | Charge |
|---|---|
| `class` | −1 300 |
| `island_chain` | −1 300 |
| `method` | −650 |
| `*_group` nodes | −700 |

### 4. Edgeless Nodes (post-simulation placement)
After the simulation ends, nodes that had no edges at all are grid-positioned below the simulated cluster:
- Y start: `max simulated Y + ISOLATED_ROW_Y_OFFSET (420 px)`
- X centre: midpoint of the simulated node X range
- Grid column gap: `ISOLATED_COL_GAP = 280 px` (or `300 px` for > 12 nodes)
- Grid row gap: `260 px` for > 12 nodes, otherwise `GRID_SNAP_ROW_GAP`
- Columns: `ceil(sqrt(n × 1.4))`, capped to the total node count

### 5. Grid Snap
After positions are finalised (simulation or cache), every node is snapped to the nearest unoccupied cell in a regular grid.

- **Column gap**: `560 px` (≤ 12 nodes), `420 px` (13–20 nodes), `340 px` (> 20 nodes) — `GRID_SNAP_COL_GAP`
- **Row gap**: `380 px` (≤ 12 nodes), `310 px` (13–20 nodes), `260 px` (> 20 nodes) — `GRID_SNAP_ROW_GAP`
- **Grid size**: `cols = ceil(sqrt(n × 1.4))`, `rows = ceil(n / cols)`
- **Single-row prevention**: if the formula above yields `rows = 1` and `cols ≥ 3`, `cols` is halved (`cols = ceil(cols / 2)`) and `rows` is recomputed. This avoids placing all nodes in a straight line, which would cause edges between non-adjacent nodes to pass through intermediate nodes.
- **Assignment**: nodes are sorted top-to-bottom, left-to-right by their pre-snap position; each is assigned to the nearest unclaimed cell (greedy nearest-neighbour); this guarantees no two nodes share a position
- **Snap animation duration**: `600 ms` — `GRID_SNAP_DURATION`

### 6. Camera / Viewport Fitting
After snapping, the viewport is adjusted to show all nodes:
- **Padding**: `20 px` on sides and bottom; an extra `30 px` at the top to clear the breadcrumb bar
- **Scale**: `min(availW / graphW, availH / graphH)` — no maximum, so arbitrarily small graphs are allowed to zoom in to fill the space
- **Animation**: the initial fit is instant; a second fit `50 ms` later is animated (`250 ms` transition)
- **Overflow guard**: after a further `100 ms`, `ensureAllNodesVisible` checks whether any node extends outside the viewport with a `30 px` margin; if so it refits with `60 px` padding and an animated transition
- **Auto-fit during simulation**: while `simulation.alpha() > 0.12`, the viewport refits every `120 ms` (no animation) to track the settling layout; auto-fit stops once the layout is stable

### 7. Node Sizes
| Node kind | Width | Height |
|---|---|---|
| `class` | 300 px | 210 px |
| `island_chain` | 300 px | 210 px |
| `method` | 210 px | 150 px |
| `*_group` nodes | 240 px | 160 px |
| Default (fallback) | 160 px | 60 px |

