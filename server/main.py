"""Application entrypoint.

Builds the :class:`~config.Settings`, creates the FastAPI application via
:func:`app.create_app`, and runs uvicorn. No application logic lives here; it is
purely the process bootstrap so the server can be started with ``python main.py``
or ``uvicorn main:app``.
"""
from __future__ import annotations

import uvicorn

from .app import create_app
from .config import Settings

app = create_app(Settings())


def main() -> None:
    """Read settings and launch the uvicorn server.

    Called when the module is run directly (``python -m server.main`` or
    ``python main.py``). Honors ``HOST``/``PORT`` from the environment.
    """
    settings = Settings()
    uvicorn.run(app, host=settings.host, port=settings.port, reload=False)


if __name__ == "__main__":
    main()
