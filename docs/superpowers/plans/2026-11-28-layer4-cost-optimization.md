# Layer 4 控制面降本与事件驱动唤醒 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate Checker 13–17 page overflow reads via a three-layer stage-review funnel (projection → `model_views` → 24 KB cap / 8000-char paging), eliminate Maker polling idle turns via event-driven `vasp-ctl watch wait`, and bump `AUTONOMY_PHASE` to `'P3_bounded_autonomy'`.

**Architecture:** All changes live strictly in Layer 4 (`control/`), Layer 3 monitoring (`monitoring/job_monitor.py`), one backwards-compatible keyword arg in `chat/toolbox_core.py`, and the external integration kit (`integrations/vasp-autonomy/` + local `.agents/plugins/`). For `vasp_get_stage_review`, stage-focused projection runs *before* `model_views.compact_result` so `same_as` references never dangle, and overflow stores the compacted view as `sha256` alongside `raw_sha256`. For waiting, `vasp-ctl watch wait` reuses `JobMonitor` + `event_outbox` with in-memory interval tightening, exponential backoff preservation, anti-reentry-loop probing, and automatic `observer -> owner` lock failover.

**Tech Stack:** Python 3.10+ stdlib, existing `vasp_agent.control` / `vasp_agent.monitoring` / `vasp_agent.chat.model_views`, `pytest`, `ruff`.

**Spec:** [docs/design/LAYER4_COST_OPTIMIZATION.md](../../design/LAYER4_COST_OPTIMIZATION.md)

## Global Constraints

- **Frozen remote engine**: Zero modifications to `ENGINE_MODULES` / `WORKER_UNITS`; `bundle.local_version()` MUST remain `'5b124e988c010ec8'` (verified via `python tools/check.py --skip-pytest --expect-release 5b124e988c010ec8`).
- **Prompt & interactive CLI compatibility**: Zero edits to `control/tool_specs.py` or `chat/prompts.py` (`PROMPT_TOKEN_BUDGET = 14800` unchanged); `Toolbox.read_context_evidence(sha256, offset)` default page size stays `3500`; `chat/model_views.py` stays untouched (`tests/test_model_views.py` and `tests/test_context_budget.py` stay green).
- **LOC gate**: Every created/modified Python file MUST stay `<= 500` lines (`warnings=0` in `tools/check.py`); `service.py` and `control/__init__.py` preserve `MAX_RESPONSE_BYTES` and `ControlService` re-exports.
- **Cross-platform determinism**: Write all source/docs/JSON files with `encoding='utf-8', newline='\n'` (LF only); never touch `.agent/`, `runs/`, `downloads/`, `reports/`, `workbench/`, `servers/`, `potentials/`, or `~/.gemini/config/`; keep `prompts/session_handoff_prompts.md` untracked.
- **Tiered test discipline**: Run only targeted `pytest` files locally (plus `tests/test_model_views.py`, `tests/test_context_budget.py`, and Windows real-lock tests in `tests/test_watch_cli.py`); never run full-suite `pytest` locally unless explicitly requested. PowerShell 5.1 forbids `&&`/`||` and `<<EOF`.

## Review Focus

1. **In-place mutation of `full_result` during stage projection** — `stage_review_view` popping/replacing nested keys directly on the caller's dict would corrupt `raw_stored` and downstream `extract_summary` / audit logs. Pinned in Task 1 (`test_projection_does_not_mutate_full_result_and_preserves_raw_digest`).
2. **`bool` accepted as `int` in numeric validation** — In Python `isinstance(True, int)` is `True`; passing `page_chars=True`, `poll_interval_override=True`, `--poll-interval` out-of-range, or `max_consecutive_errors=True` must fail with `ValueError` / `INVALID_ARGUMENTS` (`exit 2`), never treat `True` as `1`. Pinned in Task 2 (`test_read_context_evidence_validates_page_chars`) and Task 3/4 (`test_job_monitor_poll_interval_override_validation`, `test_watch_wait_rejects_invalid_arguments`).
3. **Cross-job `recovered` suppression leakage** — A newer `recovered` event for `(server, jobA, phase, id1)` must NEVER suppress an unacked `error` event for `(server, jobB, phase, id2)` or a different `phase`/`job_id` of the same job. Pinned in Task 4 (`test_watch_wait_recovered_only_suppresses_same_job_identity`).
4. **Overflow of an already-projected `vasp_get_stage_review` payload** — When a huge review payload still exceeds `24 * 1024` bytes after projection + `model_views`, `payload_overflow` must store the compacted/projected view as `sha256` (`read_tool` target), store raw `full_result` as `raw_sha256`, preserve `summary['incar_solver_diff']` and `summary['review_lease']`, and stay `<= 24 * 1024` UTF-8 bytes. Pinned in Task 2 (`test_stage_review_overflow_stores_compacted_view_and_raw_sha256`).
5. **Re-entry probe lock contention race** — If `watch wait` starts with a matching active job at `error_count >= max_consecutive_errors`, and an external daemon holds/acquires `worker_lock` before the probe runs, `check_once(task_filter=...)` sets `_last_check_skipped_locked = True`; `watch wait` must NOT crash or misreport a fresh error, and must smoothly fall into `observer` mode (`daemon_detected=True`). Pinned in Task 4 (`test_watch_wait_reentry_falls_back_to_observer_when_lock_contended`).

---

### Task 1: Stage-Focused Review Projection (`stage_review_view.py`)

**Files:**
- Create: `src/vasp_agent/control/stage_review_view.py`
- Test: `tests/test_stage_review_view.py`

**Interfaces:**
- Consumes:
  - `vasp_agent.chat.model_views.compact_result(result, store, name=None)`
  - `vasp_agent.workflow.stage_definitions` (`known_names`, `DEFINITIONS`, `entries`)
  - `vasp_agent.workflow.stage_gate.phase_of(name)`
- Produces:
  - `project_stage_review(full_result: dict[str, Any], store: Callable[[Any], dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]` — returns `(projected, raw_stored, view_meta)` for eligible stage-review dicts (those with valid `phase` in `stage_definitions.known_names()` and a dict `inputs` or `evidence`).
  - `build_stage_review_view(full_result: dict[str, Any], store: Callable[[Any], dict[str, Any]]) -> dict[str, Any]` — runs ① `project_stage_review` → ② `model_views.compact_result(projected, store, 'get_stage_review')` → fixes `model_view['full'] = raw_stored` & adds `model_view['sections_scope'] = 'projected_view'` when `model_view` exists → attaches `stage_review_view = view_meta` unconditionally (including when `len(projected) < MIN_CHARS`). For ineligible/minimal dicts, delegates directly to `model_views.compact_result(full_result, store, 'get_stage_review')`.

- [ ] **Step 1: Write failing tests in `tests/test_stage_review_view.py`**

Create `tests/test_stage_review_view.py` covering:
- `test_projection_matrix_across_all_five_phases`: build realistic 5-phase `stage_gate.context`-shaped dicts (with `inputs.initial_inputs`, `inputs.initial_poscar`, `inputs.submit_script`, `inputs.scan_verification`, `inputs.environment`, `inputs.potential_metadata`, `inputs.plan.incars`, `inputs.plan.kpoints`, `inputs.plan.parameter_notes`, `evidence.summaries` for `convergence.json`, `relaxation.json`, `geometry-summary.json`, `post-relax-validation.json`, `scf-summary.json`, plus `dos_plan`, `band_path`, `scf_occupation_audit`, `incar_solver_diff`, `review_lease`). Assert per phase:
  - `convergence`: keeps `initial_inputs`, `initial_poscar`, `submit_script`, `scan_verification`, `plan.parameter_notes`, `plan.kpoints`, `environment`, and only `convergence/*` in `plan.incars` (other stages moved to `plan.incars_omitted`).
  - `relaxation`: folds `initial_inputs`, `initial_poscar`, `submit_script`, `scan_verification`, `plan.parameter_notes`; keeps `environment`, `plan.kpoints`, `relax-*`/`validation/*` in `plan.incars`, and only `convergence.json` in `evidence.summaries`.
  - `scf`: keeps `environment`, only `scf` in `plan.incars`, folds `plan.kpoints`, keeps `relaxation.json`, `geometry-summary.json`, `post-relax-validation.json` in `evidence.summaries` (folding `convergence.json` into `evidence.summaries_omitted`).
  - `bands`: keeps `environment`, `bands` + `scf` in `plan.incars`, folds `plan.kpoints`, keeps only `scf-summary.json` in `evidence.summaries`, preserves `band_path` & `scf_occupation_audit`.
  - `dos`: keeps `environment`, `scf` in `plan.incars`, folds `plan.kpoints`, keeps **`scf-summary.json`** in `evidence.summaries`, preserves `dos_plan` & `stage_parameters`.
  - No `same_as` value anywhere in the returned view resolves to a missing/omitted path.
  - Every `omitted` hash in `stage_review_view['omitted']` and `stage_review_view['full_sha256']` round-trips cleanly from the store.
- `test_small_projected_payload_below_min_chars_still_carries_full_sha256`: craft a dict whose raw size is > 3000 chars (via bulky `initial_inputs`) but whose `scf` projected size is `< 2000` (`< model_views.MIN_CHARS`). Assert `'model_view' not in view`, `view['stage_review_view']['projected'] is True`, `view['stage_review_view']['full_sha256'] == raw_sha256`, and `full_sha256` round-trips to the unprojected full dict.
- `test_large_projected_payload_binds_model_view_full_to_raw_and_marks_sections_scope`: craft a dict whose projected size stays `>= 2000` with collapsible `manifest.hashes` / `forces` matrix. Assert `view['model_view']['full']['sha256'] == view['stage_review_view']['full_sha256']`, `view['model_view']['sections_scope'] == 'projected_view'`, and every `model_view['sections']` digest reads back the matching projected section.
- `test_projection_does_not_mutate_full_result_and_preserves_raw_digest`: snapshot `deepcopy(full_result)` before `build_stage_review_view`; assert `full_result == original` afterward and `store` holds exact unmutated `full_result`.
- `test_unknown_or_future_phase_falls_back_safely`: verify minimal/synthetic dicts without `inputs`/`evidence` pass through cleanly, and an unknown/custom `phase` does not drop `plan.incars` or `evidence.summaries`.
- `test_realistic_five_phase_views_fit_well_under_24kb`: using a full `full_workflow.prepare_full` + completed stages fixture (matching `test_model_views.py` style), assert every phase's `build_stage_review_view` JSON UTF-8 size is `< 12 * 1024` bytes.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_stage_review_view.py -q --tb=short`
Expected: FAIL (`ModuleNotFoundError: No module named 'vasp_agent.control.stage_review_view'`).

- [ ] **Step 3: Implement `src/vasp_agent/control/stage_review_view.py`**

Implement pure-Python projection module (`<= 250` lines, LF):
- Constants mapping phase → allowed `plan.incars` predicate (`convergence` → `phase_of(k) == 'convergence'`, `relaxation` → `phase_of(k) == 'relaxation'`, `scf` → `k == 'scf'`, `bands` → `k in ('bands', 'scf')`, `dos` → `k == 'scf'`) and phase → allowed `evidence.summaries` set (`convergence`: `set()`, `relaxation`: `{'convergence.json'}`, `scf`: `{'relaxation.json', 'geometry-summary.json', 'post-relax-validation.json'}`, `bands`: `{'scf-summary.json'}`, `dos`: `{'scf-summary.json'}`).
- Fallback for future stages defined in `stage_definitions.DEFINITIONS`: resolve `depends_on` summary filenames automatically and keep `phase_of(k) == phase` or dependency keys (preserving all if none match).
- Helper `_fold_section(value, *, phase, store, omitted_index, path_key, omitted_keys=None)` storing `value` via `store(value)`, recording `omitted_index[path_key] = stored['sha256']`, and returning `{'omitted_for_phase': phase, 'sha256': stored['sha256'], 'chars': stored['chars'], **({'omitted_keys': list(omitted_keys)} if omitted_keys is not None else {})}`.
- Copy-on-write shallow dict copying for `full_result`, `inputs`, `inputs.plan`, `evidence` so caller dicts are never mutated.
- Store dropped `incars` in sibling `plan['incars_omitted']` (keeping every value of `plan['incars']` a `dict` so `model_views._incar_table` stays valid) and dropped summaries in sibling `evidence['summaries_omitted']`.
- In `build_stage_review_view`: run `project_stage_review` → run `model_views.compact_result(projected, store, 'get_stage_review')` → if `isinstance(compacted, dict)` and `'model_view'` in `compacted`, shallow-copy `model_view` with `full=raw_stored`, `sections_scope='projected_view'`, and clarifying `note` → attach `compacted['stage_review_view'] = view_meta`.

- [ ] **Step 4: Run tests to verify Task 1 passes**

Run: `python -m pytest tests/test_stage_review_view.py -q --tb=short`
Expected: PASS (all tests in `tests/test_stage_review_view.py`).

- [ ] **Step 5: Commit Task 1**

Run:
```powershell
git add -- src/vasp_agent/control/stage_review_view.py tests/test_stage_review_view.py
git commit -m "feat(control): add stage-focused review projection before model_views"
```

---

### Task 2: Overflow Extraction (`service_overflow.py`), 24 KB Cap, 8000-Char Paging & `AUTONOMY_PHASE`

**Files:**
- Create: `src/vasp_agent/control/service_overflow.py`
- Modify: `src/vasp_agent/chat/toolbox_core.py:269-281`
- Modify: `src/vasp_agent/control/service.py:1-195,342-365`
- Modify: `src/vasp_agent/control/service_contracts.py:18`
- Test: `tests/test_control_service.py`

**Interfaces:**
- Consumes:
  - `stage_review_view.build_stage_review_view(full_result, store)`
  - `Toolbox.read_context_evidence(self, sha256, offset, *, page_chars=3500)`
- Produces:
  - `service_overflow.MAX_RESPONSE_BYTES = 24 * 1024`
  - `service_overflow.EVIDENCE_PAGE_CHARS = 8000`
  - `service_overflow.SUMMARY_KEYS`, `compact_solver_diff(diff)`, `extract_summary(result)`, `trim_log_tail(box, full_result, raw_bytes)`, `compact_and_cap(box, name, args, output)`
  - `service.MAX_RESPONSE_BYTES` (re-export, keeps `vasp_agent.control.MAX_RESPONSE_BYTES` compatible)
  - `service_contracts.AUTONOMY_PHASE = 'P3_bounded_autonomy'`

- [ ] **Step 1: Update & add failing tests in `tests/test_control_service.py`**

Update `tests/test_control_service.py`:
- Update L198 assertion to `assert result['autonomy_phase'] == 'P3_bounded_autonomy'`.
- Add `test_max_response_bytes_is_24kb_and_paging_is_8000_without_affecting_interactive_cli`:
  - Assert `MAX_RESPONSE_BYTES == 24 * 1024`.
  - Store a 10,000-char payload via `service.box.store_evidence(...)`.
  - Assert `service.call('vasp_read_evidence', {'kind': 'evidence', 'sha256': sha, 'offset': 0})['result']` returns `len(content) == 8000` and `next_offset == 8000`.
  - Assert direct `service.box.read_context_evidence(sha, 0)` still returns `len(content) == 3500` and `next_offset == 3500`.
- Add `test_read_context_evidence_validates_page_chars`:
  - Assert `service.box.read_context_evidence(sha, 0, page_chars=True)`, `page_chars=0`, `page_chars=20001` each raise `ValueError`.
- Add `test_stage_review_overflow_stores_compacted_view_and_raw_sha256`:
  - Monkeypatch `service.box.get_stage_review` with a realistic `scf` review dict containing both large collapsible `inputs.initial_inputs` + `manifest.hashes` AND an uncollapsible `extra_blob` pushing the *compacted* view over 24 KB, plus `incar_solver_diff` and a secret token `'sk-secret1234567890abcdef'`.
  - Assert response UTF-8 size `<= MAX_RESPONSE_BYTES`, secret scrubbed, `result['payload_overflow'] is True`, `result['sha256'] != result['raw_sha256']`, `result['chars'] < result['raw_chars']`, `result['summary']['incar_solver_diff']` preserved, `result['read_tool']['arguments']['sha256'] == result['sha256']`, reading `result['sha256']` yields the compacted/projected view (with `stage_review_view`), and reading `result['raw_sha256']` yields the full unprojected dict.
- Add `test_stage_review_normal_payload_inlines_without_overflow`:
  - Verify a 45 KB realistic `scf` `get_stage_review` payload (heavy `initial_inputs`, `incars`, `hashes`, `summaries`) returns `payload_overflow` absent and UTF-8 bytes `< 12 * 1024` in 1 call (0 paging).

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_control_service.py -q --tb=short`
Expected: FAIL on updated `AUTONOMY_PHASE` and newly added tests.

- [ ] **Step 3: Implement `toolbox_core.py`, `service_overflow.py`, `service.py`, and `service_contracts.py`**

1. In `src/vasp_agent/chat/toolbox_core.py:269-281`:
   - Change signature to `def read_context_evidence(self, sha256, offset, *, page_chars=3500):`
   - Validate `if type(page_chars) is not int or not 1 <= page_chars <= 20000: raise ValueError('证据分页大小无效。')`
   - Replace hardcoded `3500` with `page_chars`.
2. Create `src/vasp_agent/control/service_overflow.py` (`<= 180` lines, LF):
   - Set `MAX_RESPONSE_BYTES = 24 * 1024`, `EVIDENCE_PAGE_CHARS = 8000`.
   - Move `_encode_json`, `SUMMARY_KEYS`, `compact_solver_diff`, `extract_summary`, `trim_log_tail`, and `compact_and_cap`.
   - In `compact_and_cap(box, name, args, output)`:
     - When `name == 'vasp_get_stage_review'` and `isinstance(full_result, dict)`: build `compacted = {**output, 'result': stage_review_view.build_stage_review_view(full_result, box.store_evidence)}`.
     - Otherwise keep existing `COMPACT_ALIAS` / `model_views` compaction path.
     - On overflow (`raw_bytes > MAX_RESPONSE_BYTES` and not `is_evidence_page` and not `is_log_tail`):
       - Store `compacted_result = compacted.get('result')` as `view_stored = box.store_evidence(compacted_result)` and `full_result` as `raw_stored = box.store_evidence(full_result)`.
       - Populate `sha256=view_stored['sha256']`, `chars=view_stored['chars']`, `raw_sha256=raw_stored['sha256']`, `raw_chars=raw_stored['chars']`, `original_bytes=raw_bytes`, `max_response_bytes=MAX_RESPONSE_BYTES`, `summary=extract_summary(full_result)`, `preview=_encode_json(compacted_result)[:preview_limit]`, `read_tool` targeting `view_stored['sha256']`, and updated 24 KB / 8000-char note.
3. Update `src/vasp_agent/control/service.py` (brings file down to ~390 lines):
   - Import & re-export `MAX_RESPONSE_BYTES` (and import `EVIDENCE_PAGE_CHARS`, `compact_and_cap`, `_compact_solver_diff`, `_extract_summary` if needed for compatibility).
   - Delegate `_compact_and_cap` to `service_overflow.compact_and_cap(self.box, name, args, output)`.
   - In `vasp_read_evidence(kind='evidence')`: call `self.box.read_context_evidence(sha256, offset, page_chars=EVIDENCE_PAGE_CHARS)`.
4. Update `src/vasp_agent/control/service_contracts.py:18`:
   - `AUTONOMY_PHASE = 'P3_bounded_autonomy'`.

- [ ] **Step 4: Run targeted tests + interactive CLI budget checks + `tools/check.py`**

Run:
```powershell
python -m pytest -n auto --dist loadfile tests/test_control_service.py tests/test_stage_review_view.py tests/test_model_views.py tests/test_context_budget.py -q --tb=short
if ($?) { python tools/check.py --skip-pytest --expect-release 5b124e988c010ec8 }
```
Expected: All 4 test files PASS (including existing `test_read_evidence_log_enforces_12kb_cap_and_preserves_newest_tail` at 24,946 B > 24,576 B) and `check.py` PASS with `warnings=0`, `release=5b124e988c010ec8`.

- [ ] **Step 5: Commit Task 2**

Run:
```powershell
git add -- src/vasp_agent/chat/toolbox_core.py src/vasp_agent/control/service_overflow.py src/vasp_agent/control/service.py src/vasp_agent/control/service_contracts.py tests/test_control_service.py
git commit -m "feat(control): extract service_overflow with 24KB cap, 8000-char paging, and P3 AUTONOMY_PHASE"
```

---

### Task 3: `JobMonitor` In-Memory `--poll-interval` Override & Targeted `check_once(task_filter=...)`

**Files:**
- Modify: `src/vasp_agent/monitoring/job_monitor.py:68-83,268-337`
- Modify: `src/vasp_agent/control/service_watch.py:18-35`
- Test: `tests/test_job_monitor.py`

**Interfaces:**
- Produces:
  - `JobMonitor(workspace, clock=None, max_idle_wait=MAX_IDLE_WAIT, poll_interval_override=None, poll_filter=None)` — validates `poll_interval_override` is `None` or exact `int` (not `bool`) in `60..600`; computes `_effective_due(task)` purely in memory without mutating persisted `interval_seconds` or bypassing `base * 2 ** min(error_count - 1, 4)` backoff.
  - `JobMonitor.check_once(self, *, task_filter=None)` — polls only active enabled tasks matching `task_filter` when supplied.
  - `service_watch._ACTIVE_JOB_FIELDS` includes `'enabled'` so `watch_wait` detects per-task disabled state without re-parsing `job-monitor.json`.

- [ ] **Step 1: Write failing tests in `tests/test_job_monitor.py`**

Append tests to `tests/test_job_monitor.py`:
- `test_job_monitor_poll_interval_override_validation`: assert `poll_interval_override=True`, `59`, `601`, `'60'` raise `ValueError`; `60` and `600` succeed.
- `test_job_monitor_poll_interval_override_tightens_without_mutating_disk_or_breaking_backoff`:
  - Register a task with `interval_seconds=300` at fake `t=1000` (`next_due_at=1300`) and a second non-matching task on another server with `interval_seconds=300`.
  - Construct `JobMonitor(tmp_path, clock=..., poll_interval_override=60, poll_filter=lambda t: t['job_name'] == 'target')`.
  - Advance fake clock to `t=1060` and run `_check(force=False)`: assert matching task IS polled (while non-matching task is NOT polled), and persisted `job-monitor.json` still records `interval_seconds == 300`.
  - Force 2 consecutive errors on matching task: at `t=1060` (`err_cnt=1`, backoff `60s`), next due is `1120`; fail again at `t=1120` (`err_cnt=2`, backoff `120s`), assert at `t=1180` it is NOT polled (backoff preserved) and at `t=1240` it IS polled.
- `test_job_monitor_check_once_task_filter_polls_only_matching`:
  - Register two active tasks `job-a` and `job-b`; call `monitor.check_once(task_filter=lambda t: t['job_name'] == 'job-a')`; assert only `job-a` was passed to `poller` and `job-b` was untouched.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_job_monitor.py -q --tb=short`
Expected: FAIL (`TypeError: JobMonitor.__init__() got an unexpected keyword argument 'poll_interval_override'`).

- [ ] **Step 3: Implement extensions in `job_monitor.py` and `service_watch.py`**

1. In `src/vasp_agent/monitoring/job_monitor.py`:
   - Add `poll_interval_override=None, poll_filter=None` to `JobMonitor.__init__`:
     - Validate `if poll_interval_override is not None and (type(poll_interval_override) is not int or not 60 <= poll_interval_override <= 600): raise ValueError('轮询覆盖间隔必须为 60–600 秒的整数。')`
   - Add `_effective_due(self, task)`:
     - If `self._poll_interval_override is None` or `(self._poll_filter is not None and not self._poll_filter(task))`: return `task['next_due_at']`.
     - Otherwise:
       - `base = min(task['interval_seconds'], self._poll_interval_override)`
       - `err_cnt = int(task.get('error_count') or 0)`
       - `delay = min(86400, base * 2 ** min(err_cnt - 1, 4)) if err_cnt > 0 else base`
       - `anchor = task['last_checked_at'] if task.get('last_checked_at') is not None else (task['next_due_at'] - task['interval_seconds'])`
       - return `anchor + delay`
   - Use `self._effective_due(t)` in `_compute_wait(data)` and both due-checks inside `_check(force, task_filter=None)`.
   - Add `task_filter=None` keyword parameter to `check_once(self, *, task_filter=None)` and `_check(self, force, task_filter=None)`; filter `tasks` with `and (task_filter is None or task_filter(t))`. Keep file `<= 450` lines.
2. In `src/vasp_agent/control/service_watch.py:18-35`:
   - Add `'enabled'` to `_ACTIVE_JOB_FIELDS`.

- [ ] **Step 4: Run targeted tests + `tools/check.py`**

Run:
```powershell
python -m pytest tests/test_job_monitor.py tests/test_event_outbox.py -q --tb=short
if ($?) { python tools/check.py --skip-pytest --expect-release 5b124e988c010ec8 }
```
Expected: PASS, `loc` OK (`warnings=0`), `release=5b124e988c010ec8`.

- [ ] **Step 5: Commit Task 3**

Run:
```powershell
git add -- src/vasp_agent/monitoring/job_monitor.py src/vasp_agent/control/service_watch.py tests/test_job_monitor.py
git commit -m "feat(monitoring): support in-memory poll_interval_override and targeted check_once filter"
```

---

### Task 4: Event-Driven Blocking Wait (`vasp-ctl watch wait`)

**Files:**
- Create: `src/vasp_agent/control/watch_wait.py`
- Modify: `src/vasp_agent/control/watch_cli.py:1-60,233-267`
- Test: `tests/test_watch_cli.py`

**Interfaces:**
- Consumes:
  - `JobMonitor(..., poll_interval_override=..., poll_filter=...)`, `JobMonitor.check_once(task_filter=...)`
  - `event_outbox.list_events`, `event_outbox.worker_lock_held`
  - `service_watch._read_active_jobs`, `service_watch._SERVER_RE`, `service_watch._JOB_RE`
  - `service_contracts.AUTONOMY_PHASE`
  - `watch_cli._emit_json`, `watch_cli._emit_error` (or shared small helpers)
- Produces:
  - `watch_wait.run_wait(workspace: Path, *, server: str | None = None, job_name: str | None = None, phase: str | None = None, poll_interval: int | None = None, max_consecutive_errors: int = 3, wake_on_error: bool = False, max_seconds: float = 14400.0, as_json: bool = False, poller: Callable[[dict[str, Any]], dict[str, Any]] | None = None, stop_event: threading.Event | None = None, clock: Callable[[], float] | None = None, observer_interval: float = 1.0, lock_probe_interval: float = 2.0, max_idle_wait: float = 30.0) -> int`
  - CLI subcommand `vasp-ctl watch wait` wired in `watch_cli.register_watch_subparser` and `watch_cli.handle_watch`.

- [ ] **Step 1: Write failing tests in `tests/test_watch_cli.py`**

Create `tests/test_watch_cli.py` covering every state-machine branch and Windows OS lock behavior:
- `test_watch_wait_rejects_checker_and_invalid_arguments`:
  - `--role checker watch wait --json` → `rc == 1`, `error_code == 'TOOL_NOT_ALLOWED_FOR_ROLE'`.
  - `--poll-interval 59`, `--poll-interval 601`, `poll_interval=True`, `--max-consecutive-errors 0`, `max_consecutive_errors=True`, `--max-seconds 0`, `--server "bad/srv"` → `rc == 2`, `error_code == 'INVALID_ARGUMENTS'`.
- `test_watch_wait_immediate_return_on_existing_terminal_or_no_active_jobs_or_monitor_disabled`:
  - Empty workspace → `rc == 0`, `reason == 'no_active_jobs'`, `mode == 'immediate'`.
  - Pending `terminal` event already in outbox → `rc == 0`, `reason == 'event'`, `mode == 'immediate'`, `len(wake_events) == 1`.
  - Active job registered, then `monitor.configure(enabled=False)` (or task `enabled=False`) → `rc == 0`, `reason == 'monitor_disabled'`, `mode == 'immediate'`.
- `test_watch_wait_ignores_transient_error_and_wakes_on_terminal`:
  - Register active task; stub `poller` fails once (`TimeoutExpired`), then returns `RUNNING`, then returns `COMPLETED` (`terminal=True`).
  - Run `run_wait(..., max_consecutive_errors=3, max_seconds=5.0, max_idle_wait=0.05, as_json=True)`.
  - Assert `rc == 0`, `reason == 'event'`, `mode == 'owner'`, stdout contains **only one** final JSON object (silent during wait), and `wake_events` contains only the `terminal` event.
- `test_watch_wait_recovered_only_suppresses_same_job_identity`:
  - Seed outbox with `[error(jobA, id=101), recovered(jobA, id=101)]` → does NOT wake on `jobA`'s old error even with `wake_on_error=True`.
  - Seed outbox with `[error(jobB, id=202), recovered(jobA, id=101)]` → DOES wake immediately on `jobB`'s unrecovered error when `wake_on_error=True`.
- `test_watch_wait_wakes_on_same_error_deduped_threshold_via_registry_error_count`:
  - Register active task; stub `poller` raises identical `RuntimeError('ssh timeout')` 3 times (so `event_outbox` only writes 1 `error` row with `error_count=1`, while `job-monitor.json` increments `task['error_count']` to `3`).
  - Assert `run_wait(..., max_consecutive_errors=3)` exits with `rc == 0`, `reason == 'error_threshold_reached'`, `error_jobs[0]['error_count'] >= 3`.
- `test_watch_wait_owner_reentry_probes_once_and_breaks_zero_ms_loop`:
  - Pre-seed active job with `error_count=3`.
  - Subcase A (cluster recovered): `poller` returns `RUNNING` on probe then `COMPLETED` on next tick → `watch wait` resets `error_count` to `0`, does NOT immediately exit on stale threshold, and exits with `reason == 'event'` when `COMPLETED`.
  - Subcase B (cluster still broken): `poller` fails on probe → `watch wait` exits promptly with `reason == 'error_threshold_reached'` (`error_jobs[0]['error_count'] == 4`).
- `test_watch_wait_reentry_falls_back_to_observer_when_lock_contended`:
  - Pre-seed active job with `error_count=3` (and acked old error event); hold real OS `FileLock` on `worker_lock` before calling `run_wait(..., max_seconds=0.4, observer_interval=0.05)`.
  - Assert `poller` is called `0` times, `daemon_detected is True`, `mode == 'observer'`, no crash, exits cleanly with `reason == 'timeout'`.
- `test_watch_wait_observer_mode_and_automatic_failover_on_real_os_worker_lock`:
  - Hold real OS `FileLock` on `worker_lock` in a background thread/fixture for `0.25s`, then release it.
  - Run `run_wait(..., observer_interval=0.05, lock_probe_interval=0.05, max_idle_wait=0.05, max_seconds=5.0)` with a `poller` returning `COMPLETED`.
  - Assert `poller` is NOT called while lock is held, `daemon_detected is True`, and after lock release `watch wait` acquires `worker_lock`, polls, and exits with `rc == 0`, `reason == 'event'`, `mode == 'owner'`.
- `test_watch_wait_observer_unacked_vs_acked_error_and_timeout_exit_zero`:
  - Under held `worker_lock`: unacked unrecovered `error` + `error_count=3` wakes immediately (`reason == 'error_threshold_reached'`); once acked, re-entering does not spin and wakes only when external writer bumps `error_count` to `4` (or exits `rc == 0`, `reason == 'timeout'` at `max_seconds`).
- `test_watch_wait_cli_roundtrip_via_control_cli_main`:
  - Invoke `control_cli.main(['--workspace', str(ws), 'watch', 'wait', '--json'])` on workspace with no active jobs; assert `rc == 0` and valid JSON with `autonomy_phase == 'P3_bounded_autonomy'` and `reason == 'no_active_jobs'`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_watch_cli.py -q --tb=short`
Expected: FAIL (`ModuleNotFoundError: No module named 'vasp_agent.control.watch_wait'`).

- [ ] **Step 3: Implement `src/vasp_agent/control/watch_wait.py` and wire `src/vasp_agent/control/watch_cli.py`**

1. Create `src/vasp_agent/control/watch_wait.py` (`<= 300` lines, LF):
   - Strict argument validator: reject `bool` for `poll_interval`/`max_consecutive_errors`, enforce `60 <= poll_interval <= 600`, `1 <= max_consecutive_errors <= 50`, `max_seconds > 0` finite, regex checks on `server`/`job_name`/`phase`.
   - `_active_unrecovered_errors(events)`: scan `events` in chronological order keyed by `(ev['server'], ev['job_name'], ev.get('phase'), ev['job_id'])`; a `recovered` event clears earlier `error` events for that exact identity only.
   - `_evaluate_state(pending_events, active_jobs, monitor_enabled, *, wake_on_error, max_consecutive_errors, baseline_errors=None, check_error_threshold=True)`:
     - Returns `(reason, wake_events, error_jobs)` following priority:
       1. `event` (any `terminal`, or unrecovered `error` matching `wake_on_error` or `error_count >= max_consecutive_errors`);
       2. `error_threshold_reached` (when `check_error_threshold` is True: any matching active job with `error_count >= max_consecutive_errors` — or in `observer` acked-baseline mode, `error_count > baseline_errors[key]`);
       3. `no_active_jobs` (empty `active_jobs`);
       4. `monitor_disabled` (`not monitor_enabled` or all `active_jobs` have `job.get('enabled') is False`).
   - Re-entry probe before entering wait loop: if immediate check has no `event`/`no_active_jobs`/`monitor_disabled`, and matching active jobs have `error_count >= max_consecutive_errors`:
     - Build `errored_keys` and call `monitor.check_once(task_filter=...)`:
       - If `monitor._last_check_skipped_locked`: set `daemon_detected = True`, do not trigger false error; fall into observer evaluation (waking immediately only if an unacked unrecovered error exists for those errored keys, else recording `baseline_errors`).
       - Otherwise re-read outbox & active jobs; if still `error_count >= max_consecutive_errors`, exit immediately with `reason='error_threshold_reached'`.
   - Main silent loop: acquire `monitor.start(poll_fn, _on_notify)` (`mode='owner'`) or enter `mode='observer'` polling local outbox/registry every `observer_interval` and probing `monitor.start()` every `lock_probe_interval` for seamless failover; handle `SIGTERM` / `KeyboardInterrupt` / `max_seconds` / `monitor_exited`; always `monitor.stop()` in `finally`.
   - Emit single final JSON (`--json`) or concise Chinese summary on exit; exit codes `0` / `1` / `2`.
2. Update `src/vasp_agent/control/watch_cli.py`:
   - Register `wait` subparser with `--server`, `--job-name`, `--phase`, `--poll-interval`, `--max-consecutive-errors`, `--wake-on-error`, `--max-seconds`, `--json`.
   - In `handle_watch`: reject `args.role != 'maker'` with `TOOL_NOT_ALLOWED_FOR_ROLE` (`exit 1`), dispatch to `watch_wait.run_wait(...)`.

- [ ] **Step 4: Run targeted tests (including Windows OS lock tests) + `tools/check.py`**

Run:
```powershell
python -m pytest tests/test_watch_cli.py tests/test_job_monitor.py tests/test_event_outbox.py -q --tb=short
if ($?) { python tools/check.py --skip-pytest --expect-release 5b124e988c010ec8 }
```
Expected: All PASS on Windows native filesystem locks, `loc` OK (`warnings=0`), `release=5b124e988c010ec8`.

- [ ] **Step 5: Commit Task 4**

Run:
```powershell
git add -- src/vasp_agent/control/watch_wait.py src/vasp_agent/control/watch_cli.py tests/test_watch_cli.py
git commit -m "feat(control): add event-driven blocking vasp-ctl watch wait"
```

---

### Task 5: Integration Kit, Local Excluded Plugins, Guard Tests & Design Doc Sync

**Files:**
- Modify: `integrations/vasp-autonomy/SKILL.md`
- Modify: `integrations/vasp-autonomy/agents/vasp-reviewer.md`
- Modify: `integrations/vasp-autonomy/README.md`
- Modify (git-excluded local runtime copies): `.agents/plugins/vasp-autonomy/skills/vasp-autonomy/SKILL.md`, `.agents/plugins/vasp-reviewer/agents/vasp-reviewer.md`
- Modify: `docs/design/EXTERNAL_AGENT_AUTONOMY.md`
- Modify: `tests/test_autonomy_guard.py`

**Interfaces:**
- Consumes: Final CLI syntax of `vasp-ctl watch wait` (Task 4) and 24 KB / `stage_review_view` / 8000-char paging semantics (Tasks 1–2).

- [ ] **Step 1: Add `vasp-ctl watch wait` guard allow test in `tests/test_autonomy_guard.py`**

Add `'vasp-ctl watch wait --server eshell111 --job-name si-01 --phase scf --json'` and `'vasp-ctl watch wait --poll-interval 120 --max-seconds 3600 --json'` to the safe-command parametrized list in `tests/test_autonomy_guard.py` (around L302).

- [ ] **Step 2: Update integration kit docs & sync local `.agents/plugins/` copies**

1. Update `integrations/vasp-autonomy/SKILL.md`:
   - Step 4a overflow box: update `12 KB` → `24 KB`, note `stage_review_view` stage-focused projection, `sha256` (compacted view, 8000 chars/page) and `raw_sha256` (full raw payload for deep inspection).
   - Step 4c: recommend non-daemon background `vasp-ctl watch wait --server <server> --job-name <job> --phase <phase> --json` (0 LLM token during run, Reactive Wakeup on `event`/`error_threshold_reached`/`timeout`/`monitor_disabled`, ack after acting); keep `/schedule` + `vasp_watch_jobs(poll=true)` as fallback.
2. Update `integrations/vasp-autonomy/agents/vasp-reviewer.md`:
   - Rule 3: update `12 KB` → `24 KB`, document `stage_review_view` / `omitted_for_phase` pointers (on-demand deep lookup only when suspecting historical drift; no need to page omitted historical stages routinely) and `raw_sha256`.
3. Update `integrations/vasp-autonomy/README.md`:
   - Add `vasp-ctl watch wait --server <server> --job-name <job> --phase <phase> --json` usage example alongside `once`/`daemon`/`ack`.
4. Copy updated `SKILL.md` to `.agents/plugins/vasp-autonomy/skills/vasp-autonomy/SKILL.md` and updated `vasp-reviewer.md` to `.agents/plugins/vasp-reviewer/agents/vasp-reviewer.md` (verify byte-for-byte parity; both remain git-excluded via `.git/info/exclude`).
5. Update `docs/design/EXTERNAL_AGENT_AUTONOMY.md`: mark the two Layer 4 optimizations under `### 配套工程优化` as implemented (`已实施`) and cross-link `docs/design/LAYER4_COST_OPTIMIZATION.md`.

- [ ] **Step 3: Run final verification batch**

Run:
```powershell
python -m pytest -n auto --dist loadfile tests/test_stage_review_view.py tests/test_control_service.py tests/test_job_monitor.py tests/test_watch_cli.py tests/test_autonomy_guard.py tests/test_model_views.py tests/test_context_budget.py -q --tb=short
if ($?) { python tools/check.py --skip-pytest --expect-release 5b124e988c010ec8 }
if ($?) { git status --short -uall }
```
Expected:
- All 7 targeted test files PASS;
- `tools/check.py --skip-pytest --expect-release 5b124e988c010ec8` PASS (`ruff` OK, `loc` OK with `warnings=0`, `release=5b124e988c010ec8`);
- Only intended files staged/tracked; `prompts/session_handoff_prompts.md` remains untracked; `.agents/plugins/` stays git-excluded; no `.agent/` runtime files touched.

- [ ] **Step 4: Commit Task 5 (plus design & plan docs if not yet committed)**

Run:
```powershell
git add -- integrations/vasp-autonomy/SKILL.md integrations/vasp-autonomy/agents/vasp-reviewer.md integrations/vasp-autonomy/README.md docs/design/EXTERNAL_AGENT_AUTONOMY.md docs/design/LAYER4_COST_OPTIMIZATION.md docs/superpowers/plans/2026-11-28-layer4-cost-optimization.md tests/test_autonomy_guard.py
git commit -m "docs(autonomy): sync integration kit and guard tests for Layer 4 optimizations"
```
