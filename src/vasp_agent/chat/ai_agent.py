"""Natural-language supervisor facade; composes split chat modules with full re-exports."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic

import vasp_agent.workflow.vasp_band_agent as core
from vasp_agent.chat.agent_compaction import AgentCompactionMixin
from vasp_agent.chat.agent_loop import AgentLoopMixin
from vasp_agent.chat.cli import answer, followup_message, main, route_followups, session_lock
from vasp_agent.chat.prompts import INSTRUCTIONS, PROMPT_TOKEN_BUDGET
from vasp_agent.chat.tool_groups import CORE_TOOLS, GROUPS as TOOL_GROUP_DEFS, TOOL_TO_GROUP
from vasp_agent.chat.tool_schemas import (
    ANALYSIS_SCRIPT,
    ANALYSIS_TIMEOUT,
    CHECKLIST,
    EXECUTION_ONLY_KEYWORDS,
    JOB,
    LEGACY_ALIASES,
    MESH,
    MODEL_TOOLS,
    NBANDS,
    NULL_CORES_PER_TEST,
    NULL_PARALLEL_TESTS,
    NULL_PHASE,
    NULL_RESOURCE_SELECTION,
    NULL_STR,
    OFFSET,
    OPTIONAL_PATH,
    OPTIONAL_STAGES,
    PARALLEL_TESTS,
    PHASE,
    PHASES,
    PLAN_BYPASS_TOOLS,
    POTENTIAL_VARIANTS,
    RELAX_MODE,
    RESOURCE_PROPERTIES,
    RESOURCE_SELECTION,
    SCAN_ENCUTS,
    SCAN_INPUTS,
    SCAN_MESHES,
    SCAN_PRESET,
    SCAN_THRESHOLDS,
    SERVER,
    STAGE_PARAMETERS,
    STR,
    SUBMIT_TOOLS,
    TERSE_FORMS,
    TERSE_RESOURCES,
    TOOL_GROUPS,
    TOOLS,
    advertised,
    null_resource_mode,
    tool,
)
from vasp_agent.chat.toolbox_core import (
    AUDIT_INLINE_LIMIT,
    AUDIT_PREVIEW_CHARS,
    ToolboxCoreMixin,
    _SECRET_NAME,
    item_field,
    output_items,
    read_tail,
    scrub,
    scrub_payload,
)
from vasp_agent.chat.toolbox_submission import ToolboxSubmissionMixin, _SubmissionSettled
from vasp_agent.chat.toolbox_workflow import ToolboxWorkflowMixin
from vasp_agent.chat.toolbox_workspace import ToolboxWorkspaceMixin


class Toolbox(ToolboxCoreMixin, ToolboxWorkspaceMixin, ToolboxWorkflowMixin, ToolboxSubmissionMixin):
    """Unified tool registry and executor composed from focused domain mixins."""


class Agent(AgentLoopMixin, AgentCompactionMixin):
    """Conversational supervisor composed from loop and compaction mixins."""


if __name__ == '__main__':
    main()
