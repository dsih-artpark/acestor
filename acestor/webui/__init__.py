"""acestor.webui — single-page operator dashboard.

Invoke as::

    python -m acestor.webui

Serves a small FastAPI dashboard bound to 127.0.0.1 by default. Reads
scheduler state, ledger, artifacts + configs from disk — no database.

See :mod:`acestor.webui.app` for routes and :mod:`acestor.webui.__main__`
for the entrypoint.
"""
