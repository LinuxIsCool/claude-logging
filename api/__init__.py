"""
Logging Plugin API

Provides FastAPI server for search, statistics, and real-time updates.
"""

from .server import app, main

__all__ = ["app", "main"]
