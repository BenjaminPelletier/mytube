"""MyTube web application package."""

from __future__ import annotations

from typing import Any

__all__ = ["create_app"]


def create_app(*args: Any, **kwargs: Any):
    """Import and return the configured FastAPI application factory."""

    from .web import create_app as _create_app

    return _create_app(*args, **kwargs)
