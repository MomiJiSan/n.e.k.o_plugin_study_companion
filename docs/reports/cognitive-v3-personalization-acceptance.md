# V3 personalization: synthetic engineering acceptance

Date: 2026-09-10. Base checkout: `433dbba`; implementation is on its review
branch and remains uninstalled. Contract:
[ADR-0008](../adr-0008-cognitive-v3-personalization.md).

**Result: PASS — 189 related targeted tests.**
This is an engineering result. No live learner database, installed runtime
configuration, model endpoint or human trial was used. No teaching-effectiveness
claim follows from the synthetic evidence.

## What was verified

| Boundary | Evidence |
|---|---|
| Default off and compatible configuration | Strict enable/stop values, unknown version fallback, startup and hot-update settings paths, legacy positional argument preservation, existing default-off contracts |
| Evidence eligibility | Matching local learner, canonical topic and alias, hypothesis, difficulty, reviewed catalog and version; independent per-strategy samples for three endpoints |
| Conservative selection | Strong delayed evidence selects the reviewed alternate; insufficient, stale, future, conflicting, hinted, uncertified, disclosed or time-overridden evidence keeps baseline |
| Exploration | Explicit opt-in required for uncertain positive evidence; the same exposure and failure limits apply |
| Coach ownership | Only repair strategy changes; Coach veto creates no delivered cognitive question or exposure |
| Real module chain with synthetic inputs | The existing entry delivers the reviewed alternate, canonical commit creates its exposure, `KnowledgeTracker.on_answer` writes a synthetic evaluated answer, and the next server-built report/decision consumes that new outcome |
| Replay and concurrency | Startup/hot update, status, answers and personalized delivery share the answer-write lock; an exact retry remains idempotent; history and frozen-snapshot digests reject a new question after concurrent delivery, outcome or eligible-evidence changes |
| Persistent limits | Three alternate deliveries in seven days or two consecutive evaluated non-correct alternate repairs (`wrong`, `partial`, `dont_know`) stop selection; pending does not clear the streak, canonical legacy rotation events count and closing/reopening the store preserves the counters; delivery order uses `question.occurred_at, event_seq` because an unanswered question root sequence may be zero |
| Answer/outbox fallback | If the cognitive attempt event is missing, canonical `attempts` plus `evaluations` still contribute a real non-correct result to the stop; multiple canonical attempts, unsupported verdicts and canonical/cognitive disagreement fail closed |
| User stop | Blocks personalization and legacy rotation, including a stop between proposal and delivery; ordinary fallback remains available |
| Fault isolation | Evidence-read failures preserve the baseline; existing Shadow write failure and answer/outbox isolation regressions pass |
| Runtime status | `study_cognitive_personalization_status` returns all three switches, effective gates, baseline/alternate use, insufficient/stale/conflict or operational reason, seven-day alternate count, consecutive failures and user-stop state from one lock-consistent view; the last reason refers to the newest delivered cognitive repair because a Coach veto creates no committed question |

Pure policy cases use a fixed UTC clock and reproduce the same audit and report
digest for reordered identical facts. Runtime integration uses temporary
databases with relative time windows and the production entry, ledger and answer
modules. Tutor/model services are fakes; synthetic evaluation results are not
represented as human outcomes. Safety-history cases also construct synthetic
canonical events to exercise restart and limit handling independently of
optional Shadow writes.

## Independent deterministic acceptance

The standalone runner creates an isolated temporary production `StudyStore`
and exercises the production ledger, report, selector, canonical question
writer and atomic answer transaction:

```powershell
uv run python tools/cognitive_personalization_acceptance.py `
  --report-dir docs/reports
```

It writes byte-stable
`docs/reports/cognitive-v3-personalization-deterministic.json` and
`docs/reports/cognitive-v3-personalization-deterministic.md`. The report covers
production and manifest defaults, exploration off/on, strong, insufficient,
stale, conflicting, incompatible and invalid evidence, exposure and failure
stops, user stop, a frozen snapshot digest, canonical question and answer
writes, outcome projection and a rejected concurrent delivery. It records
`evidence_class=synthetic_engineering_only`,
`human_effectiveness_claim=false` and `live_database_touched=false`.

## Validation scope

The final command used `uv run python -m pytest` on these files under `tests/`:

```text
test_cognitive_personalization.py
test_cognitive_personalization_integration.py
test_cognitive_personalization_acceptance.py
test_cognitive_strategy_catalog.py
test_cognitive_strategy_store.py
test_cognitive_strategy_report.py
test_cognitive_strategy_acceptance.py
test_cognitive_v3_shadow_config.py
test_cognitive_v3_shadow_entry.py
test_cognitive_version_sets.py
test_cognitive_intervention_store.py
test_cognitive_active_question_entry.py
test_cognitive_answer_event_integration.py
test_cognitive_retention_answer_flow.py
test_cognitive_outbox.py
test_cognitive_controls.py
test_cognitive_pr0_contracts.py
test_cognitive_settings.py
test_cognitive_strategy_rotation_entry.py
test_cognitive_strategy_rotation_runtime.py
test_cognitive_strategy_rotation.py
test_cognitive_planner_v2.py
```

Result: `189 passed`. Local machine-readable output is
`.pytest-tmp-personalization/results.xml` (ignored test output, not a committed
learner artifact). Ruff passed for all changed Python files. Both configured
`pyrightconfig.json` and `pyrightconfig.services.json` suites passed, as did
explicit Pyright checks on the new production and tool modules. The independent deterministic
acceptance runner passed. No repository-wide test run, installation, live
learner database operation or human test was performed.

Pre-edit GitNexus analysis reported CRITICAL for the broadly imported
`CognitiveConfig`, HIGH for configuration parsing and canonical event insertion,
and LOW for the entry and settings changes. These risks were reported before
editing. Final change detection against the existing index returned MEDIUM and
three affected question-generation flows. Its symbol attribution includes
spurious line-range matches and is not a complete graph for newly added modules;
the actual Git diff and the above integration regressions were also checked.

## Release boundary

Personalization and exploration remain disabled in `plugin.toml`. Global stop
is available through the existing settings update path. Baseline fallback,
catalog limits and Coach ownership stay in effect. Live validation and any
decision to enable the feature are deferred until the integrated cognitive
engine is ready for that validation.

After this PR merges, V3 is engineering-complete within the currently frozen
scope. The next step is one unified installation and a complete human validation
run; those results remain separate from this synthetic engineering PASS.
