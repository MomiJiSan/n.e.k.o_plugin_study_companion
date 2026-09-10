# ADR-0008: Default-off evidence-gated repair personalization

Status: Implemented behind default-off gates; 2026-09-10

## Scope and precedence

This supersedes the prohibition on connecting evidence to candidates in
ADR-0006 only behind the new `strategy_personalization_enabled` gate. It does
not enable that gate or claim human effectiveness. Engineering acceptance
uses temporary databases and synthetic facts, with fixed clocks for pure
selection tests and relative windows for runtime integration; live validation
remains a later release activity.

The scope is the local learner, `calculus.chain_rule / omit_inner_derivative`,
an already accepted repair hypothesis, and the existing reviewed baseline
`complete_inner_derivative` and alternate `minimal_change`. Coach still merges
and may reject the candidate before blueprint validation. Only repair strategy
may change: topic, difficulty, scheduling, course, Mastery and FSRS stay owned
by existing components. No LLM selects strategies.

All three new flags default false: `strategy_personalization_enabled`,
`strategy_personalization_exploration_enabled`, and
`strategy_personalization_stopped`. Malformed enable flags mean false;
malformed stop means true. A true stop suppresses both personalization and
legacy rotation. A requested personalization mode owns strategy selection:
any failure returns the V2.1 baseline rather than falling through to rotation.
With personalization absent/false and stop false, existing rotation behavior
is preserved. Global rollback sets stop true; existing per-hypothesis user
controls and the fresh projection fence remain mandatory.

## Evidence contract

Only a server-built snapshot of the local persisted strategy ledger is an
input. Neither an uploaded report nor model text is accepted. Recompute the
deterministic v2 report from matching local learner, exact hypothesis identity,
canonical topic (including its reviewed alias), reviewed comparison family, difficulty, no-hint status,
strategy catalog v1 and engine `cognitive-v2.1-1`. Freeze the root fact boundary
and a UTC decision time. Use exposures from the preceding 30 days, and require
each strategy's newest eligible retention exposure to be within seven days.
Future-dated or conflicting exposure identities fail closed. Pending and
excluded facts cannot establish superiority.

Each of immediate, transfer and retention must have at least 20 independent
eligible exposures per strategy in the same stratum. Immediate performance
must not regress. Both transfer and retention must favor the alternate by at
least 0.10, with the alternate Wilson lower bound above the baseline upper
bound and a strictly positive missing-outcome sensitivity lower bound.
These conservative engineering thresholds are versioned policy constants,
not an assertion of causal teaching effectiveness.

Insufficient data, unknown versions, stale evidence, mismatched scope,
conflicting endpoints, invalid provenance and any read/calculation failure
keep the baseline. Uncertain evidence also keeps baseline unless the separate
exploration flag is true, all sample/freshness gates pass, both delayed point
estimates are positive, neither missing sensitivity lower bound is negative,
and immediate performance does not regress.

## Exposure limits and commit fence

At most three actual alternate repair questions may be delivered for the same
hypothesis in a rolling seven-day window, across policy versions and including
legacy rotation, abandoned and replaced deliveries. Exploration shares that
budget and is not a way to bypass it. Two consecutive evaluated alternate
repairs that are not `correct` (`wrong`, `partial` or `dont_know`) in that
window stop further alternate deliveries; intervening baseline work does not
clear the stop. A new report, restart, retry or toggle does not reset these
counters. Counters age out of the window; user stop requires explicit clearing.
A pending alternate does not erase prior failures. Delivery order is canonical
`question.occurred_at, event_seq`; question root fact sequence is not used as
the chronology because it may be zero before an answer exists.

Counts come from canonical question/attempt events, not the optional Shadow
projection. Configuration startup, settings hot update, status reads, answer
writes and personalized delivery use the existing answer-write lock whenever
they cross this boundary. The new question event rechecks the budget and
failure stop inside its existing write transaction. It compares both the
canonical history digest and the frozen strategy-ledger snapshot digest captured
at selection, so concurrent commits, results or eligible evidence changes
invalidate stale decisions. Retries of the same event remain idempotent. The
runtime rechecks live gates before commit; invalidated delivery uses the
existing safe ordinary-question fallback.

Outcome history prefers the cognitive `attempt_committed` event and falls back
to the canonical `attempts` plus `evaluations` tables when the asynchronous
cognitive outbox event is absent. A real `wrong`, `partial` or `dont_know`
therefore still contributes to the stop. Multiple canonical attempts for one
personalized question, an unsupported verdict, or disagreement between the
canonical and cognitive verdicts fails closed instead of choosing a favorable
interpretation.

Decision metadata stores policy version, reason, chosen strategy, report/root
boundary, decision time, and history digest, without answers or raw learner
identities. Proposal and question events carry the same metadata; rejected
candidates do not create exposures. Outcome writes use the existing pipeline;
the next decision rebuilds its evidence from the updated ledger. No new schema
or background worker is required.

## Runtime status

The answer-free `study_cognitive_personalization_status` entry reports all
three switches, every effective gate, whether the last delivered repair used
the baseline or alternate strategy, the current and last decision reason,
rolling seven-day alternate-delivery count, consecutive alternate failure
count, user-stop state and its root-fact boundary. Missing or unreadable ledger
state fails closed to a degraded baseline status. The status read shares the
answer-write lock, so its strategy and counters describe one consistent local
view. A Coach veto creates no committed cognitive question, so the last reason
describes the newest delivered cognitive repair rather than an uncommitted
candidate.

## Acceptance

Narrow tests must cover default-off equivalence, strong/insufficient/stale/
conflicting evidence, bounded exploration, persistent failure and exposure
limits, user stop and rollback, unknown versions, fault fallback, Coach
rejection, canonical commit race/idempotency, and a simulated
selection -> reviewed delivery -> ledger -> outcome -> updated report chain.
Only synthetic engineering results may be claimed before live validation.

## Implementation and validation

- `adaptive_learning/cognitive_personalization.py`: pure scoped evidence policy.
- `cognitive_personalization_runtime.py`: live gates, baseline fallback and
  commit-time settings check through the existing settings/Coach path.
- `store_cognitive_personalization.py`: consistent snapshot, canonical history
  counters, history plus frozen-snapshot digests, runtime summary and
  transaction fence; no new database schema.
- `entry_status_entries.py`: answer-free
  `study_cognitive_personalization_status` runtime entry.
- `tools/cognitive_personalization_acceptance.py`: independent deterministic
  synthetic acceptance runner that writes stable JSON and Markdown reports.
- `tests/test_cognitive_personalization.py` and
  `tests/test_cognitive_personalization_integration.py`: synthetic policy and
  production-module integration acceptance.

The existing settings update path accepts the three flags under `cognitive`.
For a global user stop, persist `strategy_personalization_stopped=true`; clearing
the flag does not clear history counters. Merely disabling personalization
preserves separately enabled legacy rotation, so a global stop must use the
stop flag (or disable both personalization and rotation). No settings were
enabled during implementation.

The final bounded regression passed **189 tests** on 2026-09-10. Ruff, both
configured Pyright suites, explicit checks of the new production and tool modules, and
`git diff --check` passed. The standalone deterministic runner also passed and
wrote `cognitive-v3-personalization-deterministic.json` and
`cognitive-v3-personalization-deterministic.md`. No repository-wide test,
installation, live learner database access or human trial was performed. See the
[engineering acceptance record](reports/cognitive-v3-personalization-acceptance.md).

Once the implementation PR is merged, V3 is engineering-complete within this
frozen scope. The next release activity is one unified installation followed by
the complete human validation run; that later evidence still cannot broaden
the scope or establish teaching effectiveness by itself.
