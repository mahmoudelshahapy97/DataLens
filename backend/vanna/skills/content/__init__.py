"""Packaged skill guides.

A package rather than a bare directory so ``importlib.resources`` can find it
inside an installed wheel, where there is no filesystem path to walk. The skill
directories themselves are plain folders -- their names contain hyphens and
could not be modules.
"""
