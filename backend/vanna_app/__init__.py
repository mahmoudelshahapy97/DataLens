"""The DataLens application: a multi-tenant deployment of the ``vanna`` library.

The library gives you an ``Agent`` and expects you to assemble it. This package is
that assembly, plus everything a hosted product needs that a library should not
ship: a control plane, accounts, billing, and an HTTP surface over them.

Layout
------

``config``      every environment variable, read once, validated once
``db``          the pooled control-plane connection
``migrate``     versioned schema migrations
``tenancy``     tenants, members, starters, saved queries, dashboards
``stores``      library store interfaces backed by the control plane
``accounts``    credentials, sessions, API tokens
``billing``     subscriptions and payments
``limits``      quota, rate and login throttling, shared across workers
``identity``    who is calling, and what workspace they are in
``platform``    the per-tenant agent cache
``routes/``     the HTTP surface, one module per area
``wiring``      ``create_app`` -- the composition root

Imported as ``vanna_app.*`` rather than as bare top-level modules: the previous
flat layout meant every new file had to be added to a ``COPY`` line in the
Dockerfile, and forgetting cost a ``ModuleNotFoundError`` on boot.
"""

__all__ = ["__version__"]

__version__ = "2.1.0"
