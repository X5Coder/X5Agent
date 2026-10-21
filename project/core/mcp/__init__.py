"""MCP server connections for the X5Agent console.

Public surface: ``mcp_service`` (add / list / connect / OAuth / disconnect)
plus the error type the HTTP layer maps to a status code.
"""
from __future__ import annotations

from .service import McpServiceError, mcp_service

__all__ = ["McpServiceError", "mcp_service"]
