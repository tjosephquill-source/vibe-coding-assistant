# Walkthrough Script Template (Draft)

This document defines a single, consistent slide template for all walkthroughs.

Status: design-only. This is not implemented in code yet.

## Template Rules

- Every walkthrough uses the same slide order.
- Every slide must have a clear heading.
- Keep narration concise and actionable.

## Narration Quality Rules

Every walkthrough should sound like a knowledgeable colleague explaining over coffee — clear, direct, no jargon. Imagine the listener is smart but has never read the source code.

### Voice and tone:
- **Identify the software by its well-known name first.** If it implements KNN clustering, say "KNN clustering". If it's a REST API built with FastAPI, say "FastAPI REST API". Name the algorithm, framework, pattern, or product category.
- Use everyday language, then add technical detail only when it genuinely helps.
- Translate technical descriptions into plain English — do NOT copy-paste from component docs.
- Every sentence must say something the listener can act on or learn from.

### What to avoid:
- **Vague labels instead of explanations:** "a machine learning component for finding groups in data" tells you nothing — explain HOW it works: "partitions data into K clusters where K is chosen by the user."
- **Padding with synonyms:** "clustering... grouping... putting similar things together" — pick ONE word and move on.
- **Listing too many examples:** pick 1-2 use cases max, not a laundry list.
- **Filler phrases:** "People use this kind of tool for jobs like...", "Someone doing data analysis would...", "It would be used by..."
- **Slide 1 must NOT contain** class names, method names, parameter types, or return types. But it MUST name the technology and explain its key mechanism.
- Never use consultant-speak: "posture", "orchestration layer", "contention chokepoints", "operational trustworthiness".
- Never describe the graph visualization instead of the code ("the graph shows...", "island chain...", "the nodes reveal...").
- Never hedge: "may present challenges" — say what WILL happen.

### Good narration:
- "This is a KNN clustering implementation — an unsupervised clustering technique that partitions data into K groups, where K is chosen by the user. The results can be visualized to reveal latent patterns and structure in the data."
- "This is a FastAPI REST backend for a project management app. It exposes endpoints for creating projects, assigning tasks to team members, and tracking progress against deadlines."
- "The distance calculation compares every point against every other point, so doubling your dataset quadruples the time."

### Bad narration:
- "This is a KNN clustering implementation — a machine learning component for finding groups in unlabeled numeric data. It would be used by data scientists who want quick cluster labels." (too vague — HOW does it work? What makes it distinctive? Explain the mechanism, not just the category)
- "This is KNN clustering, for grouping data points based on which points sit closest to each other. People use this kind of tool for jobs like customer segmentation, grouping similar sensor readings..." (PADDING — restating + laundry list)
- "This software groups similar data points by looking at which points sit near each other." (too vague — WHAT algorithm? Name it!)
- "This codebase is a Python clustering class, KNNClustering, that takes a NumPy sample matrix X..." (too code-level — explain the concept, not the API)

## Few-Shot Examples

These complete examples define the target quality. The LLM prompt includes them as few-shot patterns.

### Example A: Clustering Algorithm

- **Executive Overview** - This is a KNN clustering implementation — an unsupervised machine learning technique that partitions a dataset into K groups based on nearest-neighbor connectivity, where K is chosen by the user. The output can be visualized to reveal latent structure and natural groupings that aren't obvious from the raw data.
- **System Context and Boundaries** - It takes in a numeric dataset and a value for K, and outputs a cluster label for each data point. The only external dependency is NumPy for matrix operations.
- **Core Architecture and Subsystems** - The code is organized as a four-stage pipeline: first compute pairwise distances, then build a K-nearest-neighbor graph, then extract connected components from that graph, and finally assign cluster labels based on component membership.
- **Runtime / Data Flow** - You pass in your data and call fit. It computes a full distance matrix between all points, selects the K nearest neighbors for each point to build an adjacency graph, walks that graph to find connected components, then assigns each component a cluster ID — or marks it as noise if the component is too small.
- **Performance Profile** - Almost all the runtime is in the distance matrix computation, which is O(n²) — doubling the number of data points quadruples the time. The graph construction and component extraction are comparatively cheap.
- **Bottlenecks and Failure Modes** - The full distance matrix is stored in memory, so large datasets will cause out-of-memory crashes. There is no chunked or approximate mode. Passing malformed input produces unhelpful errors deep in NumPy rather than a clear validation message.
- **Dependency and Coupling Risks** - Everything is in a single class with a linear call chain, so it's easy to follow but impossible to swap out one stage independently — for example, you can't plug in a different distance metric without modifying the core pipeline.
- **Security and Reliability** - There is no input validation. Wrong data types, empty arrays, or negative K values will produce cryptic exceptions rather than helpful error messages.
- **Observability and Operability** - There is no logging or progress reporting. If clustering produces unexpected results, you have no way to inspect intermediate stages without adding your own debug code.
- **Prioritized Improvements** - First, add input validation with clear error messages for common mistakes. Second, support pluggable distance metrics so users can customize the algorithm. Third, add an approximate nearest-neighbor option to handle larger datasets without running out of memory.

### Example B: Web Application Backend

- **Executive Overview** - This is a FastAPI REST backend for an online bookstore. It handles user accounts, book catalog management, shopping cart operations, and order processing, all backed by a PostgreSQL database.
- **System Context and Boundaries** - The frontend communicates over REST endpoints. The backend reads and writes to PostgreSQL, sends order confirmation emails via SendGrid, and processes payments through Stripe's API.
- **Core Architecture and Subsystems** - There are four main modules: auth handles registration and JWT-based login, catalog manages the book inventory and search, cart tracks per-user shopping sessions, and orders orchestrates checkout, payment, and email confirmation.
- **Runtime / Data Flow** - A typical purchase flow: the user searches the catalog, adds books to their cart, then hits checkout. The orders module validates the cart, calls Stripe to charge the card, writes the order to the database, and fires off a confirmation email through SendGrid.
- **Performance Profile** - Catalog search hits the database on every request with no caching layer. For a small catalog this is fine, but with thousands of books and concurrent users it will become the bottleneck.
- **Bottlenecks and Failure Modes** - If Stripe is slow or down, the checkout endpoint blocks with no timeout — the user gets a hanging request. There is no retry logic for the SendGrid email call, so confirmation emails can silently fail.
- **Dependency and Coupling Risks** - The orders module directly calls into auth, catalog, and cart, making it a central coupling point. Changing the cart's data format would require coordinated changes in orders too.
- **Security and Reliability** - JWT tokens have no expiry configured, so stolen tokens work forever. User-supplied search queries are passed to a raw SQL query without parameterization, creating a SQL injection risk.
- **Observability and Operability** - There is basic request logging via FastAPI's middleware, but no structured logging, no metrics, and no health check endpoint. Diagnosing a failed order requires reading raw logs.
- **Prioritized Improvements** - First, parameterize the catalog search query to close the SQL injection vulnerability. Second, add a timeout and retry to the Stripe and SendGrid calls. Third, add JWT token expiry. Fourth, add a caching layer in front of catalog search.

## Standard Slide Order

### 1. Executive Overview

**Goal:** Name WHAT this software is, then explain its KEY INSIGHT — the conceptual mechanism that makes it work and what you get from using it. Two to three sentences.

- Sentence 1: "This is [well-known name] — [how it works at a conceptual level]."
- Sentence 2: What you get from it / what it reveals / what it produces.
- Sentence 3 (optional): One real-world use case.
- After naming the technology, explain HOW it works conceptually — not just a vague category label.

**Do NOT** name classes, methods, parameters, or return types. **DO** name the algorithm/technology, explain its key mechanism, and say what it produces.

### 2. System Context and Boundaries

**Goal:** Explain what goes IN and what comes OUT, and what the software depends on externally. Describe types of inputs/outputs, not parameter signatures.

### 3. Core Architecture and Subsystems

**Goal:** Explain how the software is organized — what are the main pieces and what does each piece handle? Name pieces at a conceptual level. You CAN mention module names, but always explain what they do in plain language first.

### 4. Runtime/Data Flow (Happy Path)

**Goal:** Walk through what happens when someone actually uses the software, step by step. Describe it as a story: "First... then... finally..."

### 5. Performance Profile

**Goal:** Explain where this software spends most of its time or resources, and what that means practically. Translate complexity into real-world impact.

### 6. Bottlenecks and Failure Modes

**Goal:** Explain what will go wrong and under what circumstances. Be practical — think about what a user would actually experience.

### 7. Dependency and Coupling Risks

**Goal:** Explain whether the code is well-organized or tangled, and what that means for making changes.

### 8. Security and Reliability

**Goal:** Assess whether the software handles bad input, edge cases, and adversarial use gracefully.

### 9. Observability and Operability

**Goal:** Explain how easy or hard it would be to troubleshoot this software when something goes wrong.

### 10. Prioritized Improvements

**Goal:** Recommend 3-5 specific improvements in plain language, ordered by impact. Each recommendation ties back to a problem from an earlier slide.

## Optional Slide Topics (Use If Relevant)

- Domain Model and Data Ownership
- Deployment Topology and Environment Differences
- Testing Strategy and Coverage Gaps
- Cost Efficiency (compute/storage/API usage)
- Team Ownership Map (who owns what subsystem)
- Change Hotspots and Refactor Candidates

## Per-Slide Heading Style

Use the topic name as the heading — no "Slide N:" prefix. The heading is separated from the body by a dash.

Format: `<Topic> - <body text>`

Examples:
- `Executive Overview - This is a KNN clustering implementation...`
- `Performance Profile - Almost all the runtime is in...`
- `Prioritized Improvements - First, add input validation...`
