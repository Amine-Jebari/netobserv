"""Agent service - LLM-powered investigation and runbook synthesis.

Phase 4.1 landed the RAG knowledge-base layer (``rag.py``, ``embeddings.py``,
``chunking.py``, ``db.py``).

Phase 4.2 (this module set) adds the long-running investigation agent:

  - ``main.py`` / ``__main__.py``  - FastAPI service + Kafka consumer.
  - ``agent.py``                   - LangChain Gemini agent loop.
  - ``tools.py``                   - the four agent tools.
  - ``runbook.py``                 - deterministic runbook renderer.
  - ``investigations_store.py``    - Postgres CRUD for the investigations table.
  - ``metrics.py``                 - Prometheus metrics including
                                     ``claude_tokens_used_total``.
  - ``logging_setup.py``           - JSON logging via structlog.

The LLM provider is **Google Gemini** (``gemini-2.0-flash`` by default) via the
``langchain-google-genai`` integration. The metric name keeps the ``claude_``
prefix for dashboard continuity.
"""
