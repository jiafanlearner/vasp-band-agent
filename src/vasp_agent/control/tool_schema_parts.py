"""Shared JSON Schema fragments for Layer 4 control-plane tool definitions."""
from __future__ import annotations

from vasp_agent import guides
from vasp_agent.chat.tool_schemas import (
    ANALYSIS_EXPORT_OUTPUTS,
    ANALYSIS_IMAGE_PROFILE,
    CHECKLIST,
    JOB,
    MESH,
    NBANDS,
    NULL_CORES_PER_TEST,
    NULL_PARALLEL_TESTS,
    NULL_PHASE,
    NULL_STR,
    OFFSET,
    OPTIONAL_STAGES,
    PHASES,
    POTENTIAL_VARIANTS,
    RELAX_MODE,
    SCAN_ENCUTS,
    SCAN_MESHES,
    SCAN_PRESET,
    SERVER,
    STAGE_PARAMETERS,
    STR,
)

__all__ = [
    'ANALYSIS_EXPORT_OUTPUTS', 'ANALYSIS_IMAGE_PROFILE', 'BOOL', 'BUDGET', 'CHECKLIST',
    'CONTRACT_ID', 'EVENT_ID', 'FAILURE_CATEGORY', 'GUIDE_ENUM', 'JOB', 'MESH', 'NBANDS',
    'NON_EMPTY_STR', 'NULL_CONTRACT_ID', 'NULL_CORES_PER_TEST', 'NULL_DETAILS', 'NULL_EVENT_IDS',
    'NULL_FILES', 'NULL_FIXED_ENCUT', 'NULL_INPUTS', 'NULL_JOB', 'NULL_JOB_ID', 'NULL_NBANDS',
    'NULL_NUMBER', 'NULL_OP_ID', 'NULL_PARALLEL_TESTS', 'NULL_PHASE', 'NULL_PHASES',
    'NULL_PHASE_RESOURCE', 'NULL_PROD_MESH', 'NULL_SHA256', 'NULL_STR', 'NULL_TIMEOUT', 'OFFSET',
    'OPTIONAL_STAGES', 'PHASE_REQUIRED', 'PHASE_RESOURCE', 'PHASES', 'PHYSICS_INPUT',
    'POTENTIAL_VARIANTS', 'PRECISION_BOUNDS', 'RELAX_MODE', 'REPAIR_STRATEGY', 'RESOURCE_ENVELOPE',
    'SCAN_ENCUTS', 'SCAN_MESHES', 'SCAN_PRESET', 'SCAN_THRESHOLDS_OPT', 'SERVER', 'SHA256_REQ',
    'STAGE_PARAMETERS', 'STR',
]

NULL_JOB = {'type': ['string', 'null'], 'pattern': JOB['pattern']}
NULL_JOB_ID = {'type': ['string', 'null'], 'pattern': '^[0-9]+$'}
NULL_SHA256 = {'type': ['string', 'null'], 'pattern': '^[a-f0-9]{64}$'}
SHA256_REQ = {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}
NULL_OP_ID = {'type': ['string', 'null'], 'pattern': '^[a-f0-9]{32}$'}
NULL_FILES = {'type': ['array', 'null'], 'minItems': 1, 'maxItems': 10, 'items': STR}
NULL_INPUTS = {
    'type': ['array', 'null'],
    'maxItems': 10,
    'items': {
        'type': 'object',
        'properties': {'path': STR, 'expected_sha256': STR},
        'required': ['path', 'expected_sha256'],
        'additionalProperties': False,
    },
}
NULL_TIMEOUT = {'type': ['integer', 'null'], 'minimum': 1, 'maximum': 60}
NULL_NUMBER = {'type': ['number', 'null']}
BOOL = {'type': 'boolean'}
NON_EMPTY_STR = {'type': 'string', 'minLength': 1}
GUIDE_ENUM = {'type': ['string', 'null'], 'enum': [*guides.ids(), None]}
CONTRACT_ID = {'type': 'string', 'pattern': '^[A-Za-z0-9][A-Za-z0-9_-]{3,63}$'}
NULL_CONTRACT_ID = {'type': ['string', 'null'], 'pattern': '^[A-Za-z0-9][A-Za-z0-9_-]{3,63}$'}

PHASE_RESOURCE = {
    'type': 'object',
    'properties': {
        'submission_mode': {'type': 'string', 'enum': ['allocation', 'array']},
        'parallel_tests': {'type': 'integer', 'minimum': 1, 'maximum': 6},
        'cores_per_test': {'type': 'integer', 'minimum': 1, 'maximum': 65536},
        'nodes': {'type': 'integer', 'minimum': 1, 'maximum': 256},
        'partition': STR,
        'walltime': STR,
    },
    'required': ['parallel_tests', 'cores_per_test', 'nodes', 'partition', 'walltime'],
    'additionalProperties': False,
}
NULL_PHASE_RESOURCE = {**PHASE_RESOURCE, 'type': ['object', 'null']}

RESOURCE_ENVELOPE = {
    'type': 'object',
    'properties': {
        'allowed_modes': {'type': 'array', 'minItems': 1, 'items': {'type': 'string', 'enum': ['allocation', 'array']}},
        'allowed_partitions': {'type': 'array', 'minItems': 1, 'items': STR},
        'max_parallel_tests': {'type': 'integer', 'minimum': 1, 'maximum': 6},
        'max_cores_per_test': {'type': 'integer', 'minimum': 1, 'maximum': 65536},
        'max_nodes': {'type': 'integer', 'minimum': 1, 'maximum': 256},
        'max_total_cores': {'type': 'integer', 'minimum': 1, 'maximum': 65536},
        'max_walltime': STR,
        'phase_defaults': {'type': 'object', 'minProperties': 1, 'additionalProperties': PHASE_RESOURCE},
    },
    'required': [
        'allowed_modes', 'allowed_partitions', 'max_parallel_tests', 'max_cores_per_test',
        'max_nodes', 'max_total_cores', 'max_walltime', 'phase_defaults',
    ],
    'additionalProperties': False,
}
PRECISION_BOUNDS = {
    'type': ['object', 'null'],
    'properties': {
        'max_encut': {'type': 'number', 'minimum': 200, 'maximum': 3000},
        'max_kmesh_axis': {'type': 'integer', 'minimum': 1, 'maximum': 128},
        'max_kmesh_points': {'type': 'integer', 'minimum': 1, 'maximum': 128 ** 3},
    },
    'additionalProperties': False,
}
BUDGET = {
    'type': ['object', 'null'],
    'properties': {
        'max_submissions': {'type': 'integer', 'minimum': 1, 'maximum': 200},
        'max_resume_retries': {'type': 'integer', 'minimum': 0, 'maximum': 20},
        'max_repair_retries': {'type': 'integer', 'minimum': 0, 'maximum': 20},
        'max_scan_retries': {'type': 'integer', 'minimum': 0, 'maximum': 20},
        'max_consecutive_failures': {'type': 'integer', 'minimum': 1, 'maximum': 20},
    },
    'additionalProperties': False,
}
PHYSICS_INPUT = {
    'type': ['object', 'null'],
    'properties': {
        'magnetism': STR,
        'charge': {'type': ['integer', 'boolean']},
        'functional': STR,
        'soc': BOOL,
        'hubbard_u': BOOL,
        'hybrid': BOOL,
        'confirmed_by': STR,
        'assumptions_source': STR,
    },
    'additionalProperties': False,
}
PHASE_REQUIRED = {'type': 'string', 'enum': PHASES}
NULL_PHASES = {'type': ['array', 'null'], 'minItems': 1, 'items': PHASE_REQUIRED}
NULL_DETAILS = {'type': ['object', 'null']}
SCAN_THRESHOLDS_OPT = {
    'type': ['object', 'null'],
    'properties': {
        'energy_eV_per_atom': {'type': ['number', 'null']},
        'force_eV_per_A': {'type': ['number', 'null']},
        'stress_kbar': {'type': ['number', 'null']},
    },
    'additionalProperties': False,
}
NULL_FIXED_ENCUT = {'type': ['number', 'null'], 'minimum': 200, 'maximum': 3000}
EVENT_ID = {'type': 'string', 'pattern': '^[A-Za-z0-9][A-Za-z0-9_-]{3,63}$'}
NULL_EVENT_IDS = {'type': ['array', 'null'], 'minItems': 1, 'maxItems': 200, 'items': EVENT_ID}
REPAIR_STRATEGY = {
    'type': 'string',
    'enum': ['fresh_restart', 'production_from_convergence', 'bands_from_scf', 'dos_from_scf'],
}
FAILURE_CATEGORY = {
    'type': 'string',
    'enum': [
        'array_failure', 'oom', 'scheduler_failed', 'capacity_conflict', 'node_divisibility',
        'walltime_exceeded', 'vasp_crash', 'empty_bands_insufficient', 'transient_failure',
        'incar_numerical_unsupported',
    ],
}
NULL_NBANDS = {'type': ['integer', 'null'], 'minimum': 1, 'maximum': 4000}
NULL_PROD_MESH = {
    'type': ['array', 'null'],
    'items': {'type': 'integer', 'minimum': 1, 'maximum': 128},
    'minItems': 3,
    'maxItems': 3,
}
