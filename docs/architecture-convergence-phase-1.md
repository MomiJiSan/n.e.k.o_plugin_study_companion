# Architecture convergence phase 1

Version 0.2.0 introduces typed internal contracts for the study learning
pipeline while retaining public entry payloads and the existing SQLite schema.

`QuestionApplicationService` owns the bounded question-factory invocation;
`AnswerAssessmentService` is the deterministic-assessment boundary; and an
`EvaluatedAttempt` is the sole input to `LearningCommitService`.  The
`StudyTrackerCommitAdapter` maps that attempt to the existing
`KnowledgeTracker.on_answer` call, which continues to own the atomic store
transaction, idempotency, rollback, and cancellation-drain behavior.

Hosted and static knowledge-map surfaces use `study_query_knowledge_map` with
incremental V2 pagination. `study_knowledge_map` remains a deprecated external
compatibility entry, but is not called by built-in surfaces.

Local-model assets and runtime code are archived under
`experimental/local_models` and excluded from plugin builds. Production keeps
side-effect-free compatibility entries: catalog/status report unavailable and
asset operations use the stable unavailable error code. Existing user model
files are never touched.

This phase deliberately does not change the SQLite schema, public JSON fields,
error codes outside the paused local-model compatibility surface, tutor prompts,
model routing, quotas, timeout policy, or learning algorithms.
