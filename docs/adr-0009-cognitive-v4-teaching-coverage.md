# ADR-0009: Versioned comprehensive teaching coverage

Status: Implemented in PR #105; 2026-09-10

## Decision

Teaching coverage follows V3 as a separately versioned V4 phase. The first
version, `cognitive-v4-coverage-1`, expands the reviewed chain-rule catalog from
one Active error mechanism to all three topic-specific mechanisms. The complete
version, `cognitive-v4-coverage-2`, adds deterministic teaching contracts for
every bundled knowledge-graph topic whose required source fields are present.

`cognitive-v4-coverage-2` binds:

- extractor `cognitive-extractor-v1`;
- catalog `cognitive-catalog-v3`;
- reducer `cognitive-reducer-v2.1-1`;
- projection `cognitive-v4-coverage-2`;
- validator `cognitive-question-validator-v4-coverage-2`.

The release configuration remains on `cognitive-v2.1-1`, and every cognitive
gate remains default off. Selecting comprehensive coverage requires an explicit
version and model-version change plus the existing projection, read, intent and
knowledge-graph gates. Unknown or mismatched versions continue to disable
cognitive behavior.

## Coverage model

The catalog has two content tiers:

1. Chain rule uses 12 fixed, reviewed blueprints. Its three specific error
   mechanisms each have diagnostic, repair and transfer paths.
2. Other bundled topics use deterministic blueprints compiled from the checked-in
   knowledge seed. The compiler consumes only topic name, typical misconceptions,
   skills, a concrete example with answer outline, prerequisites and a related
   topic with a stated reason. It never asks a model to invent protected scoring
   content.

The graph tier can activate three falsifiable mechanisms:

| Mechanism | Required source fields | Probe | Repair | Transfer |
|---|---|---|---|---|
| `concept_misunderstanding` | misconception, example, answer outline, related edge | state concept and conditions | correct the named misconception, then redo the example | explain the checked-in relation to another topic |
| `prerequisite_gap` | prerequisite with reason, example, answer outline, related edge | identify the required prerequisite | restore the prerequisite link before solving | reuse the prerequisite in the related topic |
| `procedure_or_representation_error` | at least two skills, example, answer outline, related edge | expose the missing process | redo the example with the required procedure | preserve and adjust steps in a related representation |

Eligibility is evaluated independently per mechanism. Missing prerequisite data
disables only `prerequisite_gap`; missing all required data leaves the topic
Shadow. Unknown and deleted topics fail closed. The checked-in seed currently
produces at least one Active path for every bundled topic.

Coach continues to own the target topic, difficulty, schedule and final candidate
acceptance. Runtime delivery and validation receive the same graph-backed catalog,
so a dynamic blueprint cannot be accepted under a different topic or version.

## Measured coverage

The deterministic audit in `tools/cognitive_teaching_coverage_acceptance.py`
loads the production seed manifest without opening a learner database. For the
2026-09-10 seed it reports:

- 11 subjects and 892/892 topics with at least one Active teaching path;
- 785 topics with all three mechanisms;
- 2,569 topic-mechanism pairs;
- 7,710 probe/repair/transfer blueprints;
- 12 fixed reviewed chain-rule blueprints and 7,698 deterministic graph
  blueprints.

The JSON and Markdown reports bind these counts to a SHA-256 digest of the seed
manifest and its 11 subject files. A changed seed must regenerate and revalidate
the report.

## Compatibility boundary

- `COGNITIVE_CATALOG_V1`, `cognitive-v2.1-1` and
  `cognitive-v4-coverage-1` keep their previous behavior.
- Generic graph hypotheses are known to the intent policy only under
  `cognitive-v4-coverage-2`; older Shadow runs do not start proposing generic
  interventions.
- V3 strategy attribution, rotation and personalization remain scoped to
  `omit_inner_derivative`.
- Retention remains scoped to `omit_inner_derivative` until a separate versioned
  retention catalog and transaction review are completed.
- The comprehensive graph tier is deterministic and source-bound, but it is not
  described as individually expert-reviewed content.

## Acceptance

Targeted acceptance must prove:

1. Every manifest topic resolves to at least one Active path under coverage v2.
2. Every Active topic-mechanism pair resolves three distinct intent families and
   round-trips by blueprint id.
3. Representative math, language, science, humanities, economics and computing
   paths pass policy, delivery and question validation.
4. The real question-entry flow uses the runtime graph catalog and bypasses model
   question generation for a deterministic Active blueprint.
5. Incomplete and unknown topics fail closed.
6. Old catalogs, old policy defaults, retention and personalization retain their
   earlier scope.

The comprehensive implementation passed all 488 `test_cognitive_*` tests,
Ruff, both configured Pyright suites and Python compilation. The independent
coverage audit returned PASS. No repository-wide test, installation, live
learner database operation or human validation was performed.

No human-effectiveness claim follows from this expansion. Installation and human
validation remain separate, and the release defaults remain inert.
