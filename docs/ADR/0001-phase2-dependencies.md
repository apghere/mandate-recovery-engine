# ADR 0001 — Phase 2 dependencies: PyYAML, FastAPI, httpx

- **PyYAML** (runtime): `data/taxonomy.yaml` needs to stay human-editable
  (multi-line-friendly, commentable) since it will grow to ~120 raw decline
  templates that get hand-tuned; JSON would work but is worse for that
  editing task, and YAML parsing is otherwise unavailable in the stdlib.
- **FastAPI** (runtime): already the named framework for `api/` and
  `simulator/` in the architecture (docs §H.1); the simulator service starts
  in this phase, so the dependency starts now rather than being deferred.
- **uvicorn** (runtime): the ASGI server FastAPI needs to actually run.
- **httpx** (dev): FastAPI's `TestClient` requires it for in-process testing
  of the simulator without a running server or network in CI.

None of these are on the CLAUDE.md never-add list (redis, celery, kafka,
langchain, langgraph, vector db, non-SQLAlchemy-Core ORM, non-Tailwind UI
lib).
