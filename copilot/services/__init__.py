"""Services subsystem: AST inspection, code search, and telemetry."""
from copilot.services.ast_service import ASTService, generate_ast_outline
from copilot.services.search_service import SearchService
from copilot.services.telemetry import (
    ErrorTaxonomy,
    TokenTracker,
    compact_output,
    estimate_tokens,
    get_recent_traces,
    get_session_diagnostics,
    get_token_tracker,
    get_token_usage,
    record_trace,
    reset_telemetry,
    reset_token_usage,
)

__all__ = [
    "ASTService",
    "generate_ast_outline",
    "SearchService",
    "ErrorTaxonomy",
    "TokenTracker",
    "compact_output",
    "estimate_tokens",
    "get_recent_traces",
    "get_session_diagnostics",
    "get_token_tracker",
    "get_token_usage",
    "record_trace",
    "reset_telemetry",
    "reset_token_usage",
]
