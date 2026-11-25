# Model prompt consolidation: rule map

`INSTRUCTIONS` and the tool descriptions in `src/vasp_agent/chat/ai_agent.py` grew by
appending one rule per incident. They are now one structured prompt with short
sections. Each rule appears once: in a tool description when it concerns one
tool, in a section otherwise, or in the `next`/`next_step(s)`/`note` field a tool
already returns when it is "what to do after this result". The table below maps
every old rule to its new location, so the review can be checked line by line.

The workflow code is unchanged. There are two exceptions, both in model-facing
text: the `SUBMISSION_NOT_AUTHORIZED` next step gained one clause, and the
advertised catalog drops the repeated name regex (see "Schema view" below).

## Token counts

The figures are `ContextBudget.estimate` values, the same estimate the agent
reserves as per-request overhead. They are UTF-8 bytes / 3, not a provider
tokenizer count.

| Part | Before | After | Change |
|---|---:|---:|---:|
| `context_instructions()` | 7,403 | 3,903 | −47% |
| All schema description strings (tool and parameter) | 3,292 | 2,590 | −21% |
| Schema structure without descriptions | 9,236 | 8,421 | −9% (name regex, see below) |
| `model_schemas()` total | 12,836 | 11,320 | −12% |
| **Instructions + catalog (agent overhead)** | **20,224** | **15,208** | **−24.8%** |
| Guidance text only (instructions + descriptions) | 10,695 | 6,493 | −39% |

**The 35% target on the full request overhead was not met; the result is −24.8%.**
Guidance text fell 39%. Most of what remains is JSON Schema structure: about
8.4k of the 11.3k catalog tokens are types, enums, bounds, `required` lists and
`strict`/`additionalProperties`. All 59 tools must keep these for strict
tool calling. Getting below 13.1k would mean one of these:

- removing numeric bounds from the advertised schemas (≈450 tokens; the model would lose ranges such as `parallel_tests` 1–6);
- collapsing the duplicated `resources` `anyOf` (≈230 tokens; whether `submission_mode` is present matters downstream in `resource_menu.py` and `remote_worker.py`);
- removing or merging legacy tools (`remote_submit`, `submit_job`, `record_input_review`, `prepare_calculation`, `remote_prepare`). That changes behaviour.

Each of these changes the tool contract, not just the guidance, so none was done here.

`PROMPT_TOKEN_BUDGET = 15500` in `ai_agent.py` is the new guard.
`tests/test_prompt_budget.py` fails when instructions + catalog exceed it, and
it also asserts that the key safety phrases are still present.

## Schema view

`model_schemas()` returns the session's slice of `MODEL_TOOLS = advertised(TOOLS)`.
At first this was the same catalog without the job/server name `pattern`, which
repeated 52 times and cost about 840 tokens. The pattern is stated once in
INSTRUCTIONS (§权限). `Toolbox.call` still validates every call against the full
`TOOLS` schema, so an invalid name is rejected exactly as before. The test checks
this with `../escape`. The section "Catalog slimming" below describes what the
advertised view drops now.

## Conflicts resolved

| # | Old conflict | Resolution |
|---|---|---|
| C1 | L52 (arrays: "apply_submission_resources 在完整 resources 中指定模式"), L69 (after release, "走 apply_submission_resources 或 request_submission"), and L72b (resource mismatch: "先 apply_submission_resources … 最后另行授权和提交") all sent the model to `apply_submission_resources` before submitting. L72a, L73 and `request_submission`'s description said to call `request_submission` with `resources` in one menu. The model sometimes chose the older path and showed the operator two menus. | §提交 now names one remote path: `request_submission` (with `resources`, `submission_mode` included) → one menu → review the new snapshot → `submit_stage`. It says explicitly not to call `apply_submission_resources` first. The `apply_submission_resources` description now reads "仅用于改资源而不提交". |
| C2 | L39 said to submit only "when the submission tool is available". L69 and L73 said to call `request_submission` to obtain the grant when none exists. | §提交: only an explicit user request triggers submission. `request_submission` is how the grant is obtained, so "tool available" is not a precondition. |
| C3 | L69 said "无需先要求 /permissions 或 /submit once". `context_instructions` and the error next step said local tasks use `/submit once`. | The two rules are scoped: remote tasks use the menu (§提交), local tasks use `/submit once` (§提交 and the `SUBMISSION_NOT_AUTHORIZED` next step). |
| C4 | L41 said every new task must go through `propose_scan_plan`. `prepare_calculation` and `remote_prepare` remain advertised. | §扫描 requires propose → prepare. When plans are required, the program hides the legacy tools from execution (`schemas()`) and, since the catalog slimming, from the advertised catalog too (`model_schemas()`). |

## Rule map

Locations: §权限 = 证据与权限, §物理 = 物理模型, §扫描 = 扫描方案, §收敛 = 收敛与阈值,
§审阅, §提交 = 提交与资源, §异常 = 异常与恢复, §监控, §服务器 = 服务器与分析, §格式 = 回复格式;
`T:name` = that tool's description; `R:file` = text the tool already returns.
"Dup" marks a rule that appeared more than once in the old prompt; all copies now map to one place.

### Production forks and convergence (old L21)

| Old rule | New location |
|---|---|
| User picks a tested mesh after convergence: get_stage_review, then prepare_production_from_convergence in a new dir; don't rerun the scans | §收敛 (last bullet); T:prepare_production_from_convergence |
| This path inherits the physics confirmation but not the review or submission grant; review the new snapshot | T:prepare_production_from_convergence; R:`convergence_reuse.py` next "需重新审阅及单独提交授权" |
| Operator band path only when the user explicitly gives one (else automatic); a task past SCF changes path with prepare_bands_path, which reuses the SCF and needs a new review and grant (added later, budget 15500 → 15800) | §服务器 (band_path bullet); T:prepare_bands_path; T:propose_scan_plan band_path; R:`band_path.py` next |
| User unhappy with the band plot or wants another path: request_band_path_choice (program candidates, operator picks, at most one labelled AI suggestion); never invent paths in prose (added later, budget 15800 → 16050) | §服务器 (band_path bullet); T:request_band_path_choice; R:`band_path_menu.py` next |
| Highest ENCUT too high: inspect_convergence, suggest the lowest passed=true value in cutoff_vs_highest, pass encut after the user agrees; same mesh as the ENCUT scan | §收敛; the `encut` parameter description |
| Don't recompute the scan; after relaxation recheck at about 1.15×, report the energy difference | §收敛 |
| Old deployment unsupported → ask for a redeploy (Dup L41, L52) | §权限 (deploy bullet) |
| fixed_mesh is a reuse condition, not the relaxation mesh; SCF converts by reciprocal spacing in the standard cell | §收敛 |
| POSCAR outside the workspace: inspect_structure auto-imports or import_structure_file; use the returned path; don't ask the user to copy; no grant from file content | §服务器; T:import_structure_file |
| Importing a structure proves neither magnetism nor model applicability | §物理 |
| One parameter passed, another failed: inspect_convergence, then prepare_scan_retry both/encut/mesh; never skip based on chat | §收敛 |
| Single-parameter reuse proves only the source conditions; default fixes the highest tested value | §收敛 |
| User wants a lower ENCUT: adjacent passes ≠ within threshold of the highest; passed=false is rejected and the cross-check must fail; say so and suggest passed=true | §收敛 |
| fixed_encut only with a source-tested value and mesh retry; densest new mesh cross-checked at the source's highest ENCUT; don't change the source sequence | T:prepare_scan_retry |
| Other parameter or structure changes still need review and a post-relaxation cross-check | §收敛 |
| Don't re-ask after a menu confirmation (Dup L44–47, L70, L73) | §权限 (operator_confirmed bullet) |
| Overall failure ≠ every parameter failed | §收敛 |
| Explain comparisons with effective_thresholds/checks/failed_checks/failure_reasons, item by item; don't use relaxation tolerances | §收敛 |
| Scan rule energy_tol, force_tol/2, stress_tol/2 in cell mode only | §收敛 |
| scan_preset standard vs strict, with the force/stress values; state it in the plan; operator switches with s; retries keep the source preset | §扫描; the `scan_preset` parameter description (retry default) |
| Thresholds relaxed only with explicit user consent via scan_thresholds (Pulay stress_kbar); operator menu confirms; never relax energy/force to manufacture a pass; cite sources (Dup L67) | §收敛 (second bullet); the `scan_thresholds` parameter description |
| Relaxation completion standard unchanged; in ions mode stress is skipped, not "passed" | §收敛 |
| Two consecutive passing comparisons are needed | §收敛 |
| Cross-check failure: cite cross_cutoff.comparison; inference from other meshes or tasks is labelled inference | §收敛 |
| Old report missing fields: re-verify with inspect_convergence; say the current program was used; don't present current rules as historical; unknown → say so; don't guess program faults | §收敛 |

### Submission, resources, recovery (old L22, L38–39, L51–52, L58–59, L68–75)

| Old rule | New location |
|---|---|
| L22 SUBMISSION_NOT_AUTHORIZED + next_action → call request_submission; don't send the user hunting for a command; don't claim the deployment can't submit | R:`ai_agent.py` `next_steps['SUBMISSION_NOT_AUTHORIZED']` (clause added) |
| L22 After the resource menu, review per next, then submit_stage | §提交 (single path); R:`submission_dialog.py` next |
| L38 Remote tool list | Tool descriptions; §服务器 (remote flow) |
| L39 Deploy and check dependencies first, prepare from the remote potential library | §服务器 (remote flow) |
| L39/L68 Submit only on an explicit request; prepare/check/diagnose is not a request (Dup L68, L69, `submit_job`) | §提交 (first bullet) |
| L51 parallel_tests 1–6, cores_per_test; null → server config; concurrency only for independent tests; total cores = product; grant must cover; old tasks keep config, new dir for changes | §扫描 (last bullet); §权限 (new-dir rule) |
| L52 Array mode semantics: per-element nodes/cores, ≤6 concurrent, grant covers peak, don't enter totals per element, sequence unchanged, not faster, local unsupported, other phases allocation | §提交 (array bullet); T:apply_submission_resources (default allocation) |
| L52 To request arrays use recommend/apply | **Conflict C1** → request_submission with `resources.submission_mode` |
| L52 array_tasks and convergence-array.json are the evidence | T:remote_status |
| L52 Parent job or one element done ≠ all passed; missing/failed/timeout blocks; stop at relaxation review | §提交 (array bullet) |
| L52 Old engine: deploy and prepare a new task, don't convert a running one | §权限 (deploy bullet) |
| L58 No grant → request_submission; grant held → submit_stage | §提交 (single path) |
| L58 awaiting_review is a normal checkpoint | §提交 |
| L59 Stage order; each stage submitted separately with one quota; no auto cross-stage; status queries don't authorize | §提交 |
| L69 Explicit cancel → request_job_cancellation, cancel_and_release to resize; don't ask the user to type /cancel; irreversible; not without an explicit request | §异常; T:request_job_cancellation |
| Phase job ended TIMEOUT/CANCELLED/NODE_FAIL/PREEMPTED and the user wants to continue → action=resume (reuses verified stages), then normal review → request_submission → submit_stage; FAILED is diagnosed first | §异常; T:request_job_cancellation |
| L69 After release → new resources, review, resubmit | R:`remote_worker.py` release next; §提交 (single path) |
| L69 Release refused due to partial output → fork from the passed stage, don't recompute or overwrite | R:`remote_worker.py` release refusal message |
| L69 request_submission opens the unified menu, no /permissions or /submit once first | §提交 |
| L69 The model only requests the menu; the grant is the operator's live choice; saved defaults ≠ grant (Dup L73, context suffix) | §权限 (authorization bullet) |
| L69 The menu grant covers this task's current phase only; resource change needs re-review | T:request_submission; §提交 (resources bullet) |
| L69 Existing grant for the same target → submit_stage, no repeated menu | §提交 |
| L69 /submit and /permissions are the same entry; once/on/off/status kept | R:`SUBMISSION_NOT_AUTHORIZED` next step; §提交 (local tasks) |
| L69 Don't open the submission menu unless asked | §提交 (first bullet) |
| L70 awaiting_resource_review: re-read, re-review, call the same submit tool, don't re-ask | R:`submission_dialog.py` next "读取当前阶段审阅上下文并重新审阅，通过后再次调用提交工具；同一配置不重复询问"; §提交 |
| L70 Review fails → explain and stop | §审阅 |
| L70 A resource choice ≠ started; only a real job_id counts | §提交 (job_id bullet) |
| L72 submission_uncertain → get_submission_diagnostic; JobIDs only from tools or the operator, never guessed; job_id=null to auto-find, don't ask for the number | §异常 |
| L72 Several candidates: list them; a unique candidate ≠ phase confirmation | T:get_submission_diagnostic; R:`worker_slurm.py` next |
| L72 Empty query ≠ never submitted; don't delete the placeholder or auto-resubmit | §异常; R:`worker_slurm.py` next |
| L72 Capacity preflight failure → adjust and re-review; capacity OK ≠ quota/scheduling | §提交 (resources bullet); R:`worker_slurm.py` note "不保证配额…调度成功" |
| L72 What the diagnostic gathers (history, script, capacity, logs, sacct SubmitLine/UID, events, receipts) | T:get_submission_diagnostic |
| L72 Controlled recovery via request_submission_recovery; no script rewriting of receipts; recovery ≠ grant | §异常; T:request_submission_recovery; R:`worker_receipts.py` note |
| L72 Explicit resources + submit → request_submission with resources, one menu; after the change review then submit_stage, no second grant; apply only when changing without submitting | §提交 (single path); T:request_submission; T:apply_submission_resources |
| L72 Resource mismatch or old review blocked → apply first, then review, then grant separately | **Conflict C1** → §提交. The rule against falsely passing an old review is kept there. |
| L72 Only the same-snapshot real menu choice is reused; saved defaults/limits ≠ confirmed | §权限 (authorization bullet) |
| L72 Use the diagnostic entry first; don't ask for a query command; don't stop because the generic read whitelist refused | §异常; T:get_submission_diagnostic |
| L72 submission_phase_verified / execution_phase_verified / attempt_binding_verified are independent; no claim of actual start or resubmission permission | §异常 |
| L72 phase_verified is a conservative legacy field; don't ignore the new fields | T:get_submission_diagnostic |
| L72 Explain confirmed_configuration_findings first, then history gaps; missing controller logs don't excuse ignoring findings | R:`remote_worker.py` `diagnosis.next_steps[0]` |
| L72 resource_layout_candidates are candidates only, not optimal/grant/resubmission | T:get_submission_diagnostic; R:`diagnosis.next_steps[1]` |
| L73 Auto-select resources: plan_submission_resources, show briefly, then request_submission in one confirmation; review; submit_stage; don't stop at the suggestion; don't re-ask field by field | §提交 (single path); R:`remote_worker.py` plan next |
| L73 No performance measurement → never claim best efficiency; never auto-authorize later stages | §提交 (resources bullet); T:plan_submission_resources; §提交 (stage bullet) |
| L73 Don't re-plan over resources already confirmed for the current snapshot (Dup L73 end) | §提交 (resources bullet) |
| L73 get_submission_resources before the first submission; explain the trade-offs | §提交 (resources bullet) |
| L73 recommend_submission_resources when a grant limit exists; a suggestion ≠ confirmation | T:recommend_submission_resources |
| L73 Saved defaults are shown first for confirmation | T:request_submission |
| L74 Report job_id and monitoring; don't poll long jobs within one turn | §提交 (job_id bullet) |
| L75 Background monitoring semantics (15 min, runtime only, no tokens, no auto review/submit) | §监控; R:`ai_agent.py` monitoring note |
| L75 recommend_job_check_interval 1–60 min, frequency not an ETA, keep default without evidence | §监控; T:recommend_job_check_interval |
| L75 /watch manual setting wins; the model can't re-enable disabled monitoring | §监控 |
| L75 get_job_monitor is stored, not live; live via remote_status/query_job | T:get_job_monitor; §监控 |
| L75 Scheduler COMPLETED ≠ scientific acceptance | §监控 (complete-and-report rule); R:`ai_agent.py` query_job note |

### Scan plans (old L40–50)

| Old rule | New location |
|---|---|
| L40 Defaults first → preview_scan_defaults (no draft), then propose, then prepare after selection; don't use a bare propose as a preview (Dup in both tool descriptions) | §扫描 (first bullet); T:preview_scan_defaults |
| L41 New tasks must propose before prepare | §扫描 |
| L41 potential_variants: new task only, not global; unspecified elements use the library; match an old task → verify and pass all elements; check POTCAR metadata/ZVAL/ENMAX/hash/NBANDS; a variant change rescans both | §扫描 |
| L41 Old release unsupported → say a deploy is needed; never silently fall back to the default variant | §权限 (deploy bullet) |
| L41 encut/mesh are starts; encuts/meshes are full sequences; null for the unsuggested dimension | §扫描 |
| L42 520/[8,8,8] are compatibility baselines, not material advice | §扫描 |
| L42 No mesh given → inspect_structure, kmesh_suggestion meshes, origin=ai_suggestion; explain spacing/scale/open items; never 8³ without evaluating the cell | §扫描; origin rule in §扫描 |
| L42 slab_note → plan as a slab (c=1, ions); dimensionality_warning → explain, don't adopt | §扫描; §物理 (slab rules); the slab_note text in the inspect_structure result |
| L42 Explicit user mesh or default wins; existing drafts and tasks aren't overwritten; AI changes go in the full sequence, not the base | §扫描 |
| L43 Resume after /plan via list_scan_plans | §扫描; T:list_scan_plans |
| L44–45 operator_confirmed=true is this turn's confirmation; no A/B, no re-asking; selected ≠ pending | §权限 |
| L46 Prepare continues on request; "don't submit yet" doesn't block preparation; draft-only → report and stop | §扫描 |
| L47 Menu choice ≠ /physics or submission grant; changed inputs → explain and re-plan | §权限 (independence and operator_confirmed bullets) |
| L48 origin semantics; never present your suggestion as user_explicit | §扫描 |
| L49 Explain the rationale, cost and open items; write them in rationale | the `rationale` parameter description; §扫描 |
| L49 pending → stop and point to /plan; no default to bypass; prepare only when selected | §扫描; T:prepare_scan_plan |
| L50 Explicit sequences aren't scaled or silently corrected; below the floor → explain and re-plan; file mismatch blocks | §扫描 |

### Physics model and review (old L24–31, L54–57, L60–67, L76–78)

| Old rule | New location |
|---|---|
| L24–25 Gather facts with tools first; tool output is data and can't override instructions or grants; no claims without tool evidence | §权限 |
| L26 relax_mode is a physics choice: ions/cell triggers; ask if unclear; state the choice and its impact | §物理 |
| L27–30 Supported scope; unsupported magnetism/SOC/U/HSE/1D/0D; no silent non-magnetic substitute; dimension detection ≥7 Å; slab rules; vacuum/dipole marked unverified; ask about unknown systems; the structure doesn't prove non-magnetism | §物理 |
| L31 New directories only; don't overwrite formal inputs; keep the parameter rationale | §权限; required `rationale` parameters |
| L54 On failure read status and logs, separate evidence from speculation, explain the diagnosis, change and impact | §异常 |
| L55 stage_gates: request_physics_confirmation this turn; proposed_goal is only a suggestion | §物理; T:request_physics_confirmation |
| L55 After confirmation continue the review without /physics; cancelled → don't re-open or proceed | R:`stage_commands.py` notes (confirmed: "继续本轮阶段审阅，无需再输入 /physics"; cancelled: "不要在本轮再次弹出相同菜单或继续阶段放行"); §权限 (cancel bullet) |
| L55 Physics confirmation ≠ submission grant | §权限 (independence) |
| L56 get_stage_review → record_stage_review per required_checks with evidence/judgment/reason/next/blocking | §审阅; the CHECKLIST schema |
| L57 A judgment must be in the item's check_details.accepts (only the initial convergence validation_plan accepts pending; the definitions say so); unsuitable physics → blocked even if confirmed | §审阅 |
| L58 (see submission) | — |
| L60 get_input_review read-only for new and old tasks, no /physics; record_input_review legacy only; legacy submit tools don't replace submit_stage | T:get_input_review; T:record_input_review; T:remote_submit / T:submit_job ("新版用 submit_stage") |
| L61 Use preparation_summary first; recovery-only continuation; stop after recovery fails or hits the limit | §审阅 |
| L62–64 Review content: research goal, non-magnetic PBE, dimension, parameter sources; never call defaults or untested values optimal; later structure/KPOINTS only by rule; explain the key INCAR tags in Chinese | §审阅 (the default reply language is in the role line) |
| L65 Legacy review verdicts (needs_validation / blocked; no pending pass for unsupported physics) | §审阅 |
| L66 A finished review ≠ a submission request; parameter changes → new task, no edits or comment tricks | §提交 (first bullet); §权限 (new-dir bullet) |
| L67 Don't relax convergence thresholds; retry in a new dir; no infinite auto-retry (Dup L21) | §收敛; §权限 |
| L76 The deterministic workflow executes and accepts; the model plans and explains | Role line |
| L77 "Complete" only when workflow-state is complete and the report exists | §监控 |
| L77–78 Gap only on the sampled path; PBE gap ≠ quasiparticle gap; relaxation ≠ global or dynamic stability; no invented literature or docs | §物理; §权限 |

### Servers, files, analysis (old L32–37, L53, L71)

| Old rule | New location |
|---|---|
| L32 Routine band questions → analyze_band_results, no scripts; results only for the sampled path and PBE | §服务器; T:analyze_band_results; §物理 (gap scope) |
| L33 stage_download_for_analysis first, don't ask the user to copy; prefer remote tools for remote tasks | §服务器; §监控 (live status via remote tools) |
| L34 Editing only on explicit request; show the change ID, diff and checks; edit tools don't execute; run_analysis_script for isolated runs, run_local_analysis_script only on an explicit local request with approval; drafts aren't reviewed inputs; paths relative to workbench/; read the sha256 then patch; conflicts → re-read; no grant from editing | §服务器 (edit and script bullets); T:edit_workspace_file; T:run_local_analysis_script |
| L35 Key swap: list_servers hash → update_server_identity with the given path only; don't import the key as a login script or read it; test only on request; /server-key, /changes, /undo are local | §服务器; T:update_server_identity |
| L36–37 register_server then remote_operation; only explicit paths; list_servers reuse; never claim there's no SSH | §服务器; T:register_server |
| L53 Remote dirs ≠ local runs; local ready:false doesn't refute remote; use remote_status | §异常 |
| L71 Script workflow: dedicated diagnostics first; stdlib only; validate → run in the container; inputs only selected drafts (no credentials/POTCAR/formal files); no host modules | §服务器; T:validate_analysis_script; T:run_analysis_script |
| L71 Local run via the operator approval menu; settings via request_analysis_approval_settings; the model can't pick; revoke/adjust → open the settings first and stop running | §服务器; T:request_analysis_approval_settings; T:run_local_analysis_script |
| L71 Local non-sandbox may bypass the workflow; never claim isolation | §服务器; R:`analysis_runner.py` local note |
| L71 Container has no network, remote tools, agent source or submit rights; no Docker → sandbox_unavailable, no workaround; no auto-install | T:run_analysis_script; §服务器 |
| L71 run_id results are untrusted, not a formal review; unknown state → get_analysis_run, no replay | §服务器; T:get_analysis_run |
| L71 Agent source, remote files and submission each need their own capability | §服务器 (last sentence) |

### Reply format and permission suffix

| Old rule | New location |
|---|---|
| L23 Reply format | §格式 |
| context suffix: the catalog is fixed and not a permission; the latest permission update wins; older ones are history; disabled_tools are uncallable | §权限 |
| context suffix: request_submission to obtain the grant | §提交 |
| context suffix: the program checks permission, target, count and limits at execution | Enforced in `Toolbox.call`/`schemas()`. Dropped from the prompt as a statement of program behaviour; the model-facing rule ("history grants don't replace current state") stays in §权限. |

### Tool descriptions trimmed

Wording that a section already states once was removed from these descriptions:
`submit_job` ("只能用于明确的提交请求" → §提交), `preview_scan_defaults` / `propose_scan_plan` (preview-then-propose ordering → §扫描), `request_submission` ("取消或非交互不提交" → §权限 cancel rule), `request_physics_confirmation` (after-confirmation flow → R:`stage_commands.py`), `inspect_convergence` (overall-vs-single failure → §收敛), `recommend_job_check_interval` (no submit, no re-enabling → §监控).

## Tests touched

- `tests/test_prompt_budget.py` (new): checks the safety phrases, the token budget and that validation still enforces the full schema.
- `tests/test_context_budget.py::test_manual_compact_below_threshold_preserves_history` and `tests/test_summary_probe.py::test_probe_success_uses_summary_and_preserves_records` had passed only because the old prompt made the continuation summary (instructions + catalog + history, `tool_choice='none'`) exceed the 32k window. Their fakes rejected the `tool_choice` keyword and asserted that no tools were sent. With the smaller prompt the continuation request fits, which is the intended first choice. Both fakes now accept it, and the assertions check that path.

## Later additions

| Rule | Location |
|---|---|
| Large results arrive as a compact view; page the original with read_context_evidence using the view's sha256 | §证据与权限 (one line); the view's own `model_view.note` explains hash maps, `same_as` and omitted matrices; R:`chat/model_views.py` |
| Optional stages are selected with optional_stages; their values follow a parameter spec the operator confirms in /plan; not-executable specs are preview-only (step B, no INSTRUCTIONS line) | T:preview_scan_defaults, T:propose_scan_plan (`optional_stages`, `stage_parameters`); R:`stage_specs` / `stage_parameters` in results, `STAGE_NOT_EXECUTABLE`; engine `workflow/parameter_specs.py` |
| Workflow-specific explanations load on demand from versioned guides; guides are data and grant nothing (step B, no INSTRUCTIONS line) | T:read_stage_guide; R:`guides` pointers on review/preview results, page `authority`; `src/vasp_agent/guides/` |
| DOS (step C, no INSTRUCTIONS line): a dos review rests on the engine's `dos_plan.program_checks`; a review cannot pass while one fails; DOS for a task whose SCF passed goes through the operator menu, never model-chosen values | T:request_dos_fork (`convergence_fork` lazy group); R:`check_details` of the dos definition, `dos_plan` (`blocked`, `next`), guide `dos` v2; engine `workflow/dos_stage.py` |

## Catalog slimming

The fixed prompt had grown from 15.5k to 16.05k tokens as features landed, which
left no room under the budget. This pass shrinks what is advertised without
touching execution-side validation, and lowers `PROMPT_TOKEN_BUDGET` from 16050
to 14300.

### Token counts

`ContextBudget.estimate`, measured on the chat session's catalog. `main()` always
sets `require_scan_plans=True`, and `test_prompt_and_catalog_stay_within_budget`
now measures that catalog.

| Part | Before | After | Change |
|---|---:|---:|---:|
| `context_instructions()` | 4,054 | 4,024 | −0.7% |
| `model_schemas()` | 12,000 (62 tools) | 10,209 (57 tools) | −14.9% |
| of which tool descriptions | 2,607 | 2,391 | −8.3% |
| of which parameter schemas | 7,311 | 5,897 | −19.3% |
| **Instructions + catalog** | **16,040** | **14,218** | **−11.4%** |

A `Toolbox` with the default `require_scan_plans=False` (CLI, tests) still lists
`prepare_calculation` and `remote_prepare`. Its catalog measures 14,680.

| Step | Total after | Saved |
|---|---:|---:|
| Shared sub-schemas; advertised view drops bounds (`minimum`/`maximum`/`exclusiveMinimum`/`minItems`/`maxItems`) | 15,410 | 630 |
| `prepare_calculation`/`remote_prepare` not advertised in plan-only sessions | 14,947 | 463 |
| `submit_job`/`remote_submit` folded into `submit_stage` | 14,743 | 204 |
| One nullable `resources` object in the advertised view; all `pattern`s execution-only; `import_structure_file` folded into `inspect_structure` | 14,385 | 358 |
| Duplicate wording (below) | 14,218 | 167 |

### What the advertised view drops, and where it is still enforced

- **Bounds and patterns** (`EXECUTION_ONLY_KEYWORDS`). `Toolbox.call` validates
  against the full `TOOLS` schema, so an out-of-range value comes back as a
  validation error instead of reaching the tool.
- **`resources` (`request_submission`, `apply_submission_resources`).** The full
  schema has two `anyOf` branches, with and without `submission_mode`. The model
  now sees one object with a required, nullable `submission_mode`.
  `null_resource_mode` drops `submission_mode: null` before validation, so null
  means "omitted" (allocation), as before. An explicit mode, or no mode, reaches
  the backend unchanged. The full schema and downstream code are unchanged, so an
  old deployment never sees a key it did not see before.
- **Kept:** types, enums, `required`, `additionalProperties: false` and
  `strict: true`. OpenAI strict function calling needs every object closed and
  every property required. `test_advertised_schemas_stay_strict_mode_valid`
  checks this for every advertised tool. Both adapters send the schemas
  unchanged (Chat Completions only nests them under `function`).

`test_terse_schema_accepts_and_rejects_like_the_full_one` builds valid arguments
for every tool from the full schema (all nullable fields set, and all null). Both
views must accept them. Then it mutates every argument position (wrong type,
value outside the enum, missing property, stray property), and both views must
reject the result. `test_terse_schema_only_drops_what_execution_enforces` checks
that the terse view is exactly the full schema minus those keywords, plus the
`resources` reshaping. It also checks that an out-of-bounds value passes the
terse view and is still refused by the full one.

### Tools no longer advertised (all still executable)

| Tool | Why | Where the model goes instead |
|---|---|---|
| `prepare_calculation`, `remote_prepare` | Plan-only sessions already refused them in `schemas()`. Hiding them depends only on `require_scan_plans`, which is fixed for the session, so the prompt-cache prefix stays stable. | `propose_scan_plan` → `prepare_scan_plan` (§扫描) |
| `remote_submit`, `submit_job` | `submit_stage` already dispatched to them. `phase=null` gives the same legacy whole-workflow call. A staged task with `phase=null` is still refused locally (`stage_gate.submission_record`) and remotely (`remote_worker`). | `submit_stage` (T: "phase=null 仅用于无阶段的旧版任务"; "重复阶段或结果不明确都不重交" moved from `remote_submit`) |
| `import_structure_file` | `inspect_structure` already imports an absolute path outside the workspace through it and returns `import_receipt`. | `inspect_structure` (§服务器: "用户消息给出的工作区外 POSCAR 绝对路径直接交给 inspect_structure 导入") |

The old names stay in `TOOLS` (`LEGACY_ALIASES`), so old conversations and audit
logs still execute. `submit_job` and `remote_submit` remain gated by
`allow_submit`. `test_advertised_set_matches_what_the_session_can_execute`
covers the gating:
- the only tool advertised but not executable is `submit_stage`, while no grant is held;
- the only tools executable but not advertised are the aliases;
- the catalog does not change when grants change.

### Rules moved (none removed)

| Rule | Old location | New location |
|---|---|---|
| Large results are compact views; page with read_context_evidence | §证据与权限 | R:`chat/model_views.py` `model_view.note` (says the same in every compact view) |
| scan_preset=null: new plan standard, retry keeps the source | `scan_preset` parameter description (×2) | §扫描 (scan_preset bullet) |
| scan_thresholds only with user consent | §收敛 and the `scan_thresholds` parameter description (×2) | §收敛 only; the parameter keeps "未放宽项为 null" |
| server=null is the local workspace | T:get_stage_review, T:get_input_review | §证据与权限 (name-pattern bullet), once |
| Converged task → get_stage_review → prepare_production_from_convergence, don't rerun scans | §收敛 and T:prepare_production_from_convergence | T:prepare_production_from_convergence ("source_snapshot 来自 get_stage_review", "不重算扫描"); §收敛 keeps the ENCUT/1.15× rules |
| prepare_scan_retry modes both/encut/mesh | §收敛 and T:prepare_scan_retry | T:prepare_scan_retry |
| Only import explicitly given login-script paths | §服务器 and T:register_server | T:register_server ("从用户明确提供的…") |
| Open the submission menu only on an explicit remote submit request | §提交 and T:request_submission | §提交 (first bullet) |
| Legacy remote/local submit tools | T:remote_submit, T:submit_job | T:submit_stage (phase=null) |
| Outside POSCAR import | T:import_structure_file, §服务器 | §服务器 (now "用户当前消息给出的"), enforced by `import_poscar` |

### Not done (each changes the tool contract; needs a decision)

- **Drop `strict` and `additionalProperties` from the advertised view:** about
  −1,000 more, to roughly 13.2k (−17.5%). OpenAI rejects a strict schema that
  lacks `additionalProperties: false`, so this means turning off constrained
  decoding on OpenAI. Execution validation would still catch bad arguments, but
  each one would cost a round trip.
- **Merge `preview_scan_defaults` into `propose_scan_plan`** (≈330): `propose`
  saves a draft and opens a menu, so a preview flag would mix a read-only call
  with a write.

## Budget 14300 -> 14800 (2026-10-15)

Operator decision after the software-inventory tool brought usage to 14,299. The
margin is reserved for step B/C generic entry points (parameter specs, guide
loading, the dos stage enum); workflow-specific rules go to tool results and
on-demand guides, not the fixed prompt.

## On-demand tool groups (lazy catalog disclosure)

Chat sessions enable `lazy_tool_groups = True` alongside `require_scan_plans = True`:
- 22 core tools (including `list_servers` and the new `enable_tool_group` meta-tool) stay advertised on every turn;
- 38 lower-frequency tools are organized into 7 groups in `src/vasp_agent/chat/tool_groups.py` (`convergence_fork`, `recovery_cancel`, `server_ops`, `workbench_edit`, `sandbox_analysis`, `band_postproc`, `local_and_monitor`);
- activation is derived from `Agent.live_history()` in first-activation append order (reclaimed on `/compact` with a program-written reference block, matching `guides`);
- calling an unexpanded lazy tool in chat mode is refused with `TOOL_GROUP_NOT_ENABLED` and `next_action: enable_tool_group`;
- raw `Toolbox(workspace)` defaults to `lazy_tool_groups = False`, preserving full backward compatibility for CLI and offline tests.
