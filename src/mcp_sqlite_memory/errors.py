"""Exceptions whose message is meant for the agent.

Anything derived from ToolFailure is converted into an MCP ``ToolError`` by the
audit span, so the text reaches the model verbatim and it can self-correct.
"""


class ToolFailure(Exception):
    """Base class: the message is safe and useful to show to the agent."""


class UserError(ToolFailure):
    """Bad arguments: unknown table, empty pattern, missing key column, ..."""


class PolicyViolation(ToolFailure):
    """The statement performs an action the calling tool does not allow."""


class QueryTimeout(ToolFailure):
    """The statement ran past the wall-clock budget and was interrupted."""


class UpdateGuard(ToolFailure):
    """An UPDATE touched more rows than write_query permits; it was rolled back."""
