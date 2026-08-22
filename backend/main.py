r"""Local entry point: ``uvicorn main:app --reload``.

The image runs ``uvicorn vanna_app.wiring:application --factory`` and that remains
the canonical way in -- ``--factory`` exists so importing the module does not build
an application from whatever happens to be in ``os.environ``, which is what lets
tests call ``create_app(settings)`` with their own configuration.

This file is the short form of the same thing for a laptop, because
``uvicorn main:app --reload`` is what people type and remembering the factory
invocation is a poor use of anybody's attention. Both commands produce the same
application; the only difference is that this one builds it at import.

    cd backend
    ..\.venv\Scripts\activate     # Windows;  source ../.venv/bin/activate elsewhere
    uvicorn main:app --reload --port 8000

Nothing is installed to make this work: ``vanna`` and ``vanna_app`` sit next to this
file as plain packages, so the working directory is the only thing on sys.path that
matters.
"""

from __future__ import annotations

from vanna_app.wiring import application

#: The ASGI application. Built at import, from the process environment, which is
#: exactly what ``--reload`` wants: a new process per reload, so a changed .env is
#: picked up without a second mechanism to notice it.
app = application()
