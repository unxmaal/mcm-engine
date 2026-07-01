"""Actor resolution for rule provenance (issue #10).

One helper, one order, used by every rule tool that records provenance:

    1. an explicit `actor` argument passed to the tool
    2. the ``MCM_ACTOR`` environment variable
    3. the transport principal (bearer-token identity) when the HTTP
       transport is in use
    4. the literal ``"nobody"``

The transport principal is carried in a ContextVar so it is reachable
from inside an MCP tool call without threading the request object all
the way down. The streamable-HTTP middleware sets it per request
(see transport.BearerTokenMiddleware); stdio and test contexts leave it
unset, so ``get_transport_principal()`` returns None there.
"""
from __future__ import annotations

import contextvars
import os
from typing import Optional

# Set by the HTTP transport middleware for the duration of a request.
# Default None: stdio / test / unauthenticated contexts have no principal.
_current_principal: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "mcm_current_principal", default=None
)


def set_principal(principal: Optional[str]) -> "contextvars.Token[Optional[str]]":
    """Bind the transport principal for the current context. Returns the
    reset token so callers may restore the prior value."""
    return _current_principal.set(principal)


def reset_principal(token: "contextvars.Token[Optional[str]]") -> None:
    _current_principal.reset(token)


def get_transport_principal() -> Optional[str]:
    """The bearer-token principal for the in-flight request, or None when
    no HTTP transport principal is bound (stdio / tests)."""
    return _current_principal.get()


def resolve_actor(explicit: str) -> str:
    """Resolve the actor attributed to a rule mutation. See module docstring
    for the resolution order. Always returns a non-empty string; the terminal
    fallback is ``"nobody"``."""
    if explicit:
        return explicit
    env_val = os.environ.get("MCM_ACTOR", "").strip()
    if env_val:
        return env_val
    principal = get_transport_principal()
    if principal:
        return principal
    return "nobody"
