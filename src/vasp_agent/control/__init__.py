"""Layer 4 headless control plane for external agents (Antigravity / Codex)."""
from __future__ import annotations

from vasp_agent.control.service import (
    MAX_RESPONSE_BYTES,
    ROLES,
    ControlService,
    tool_definitions,
)

__all__ = [
    'ControlService',
    'MAX_RESPONSE_BYTES',
    'ROLES',
    'tool_definitions',
]
