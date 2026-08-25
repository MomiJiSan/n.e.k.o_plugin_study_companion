# Knowledge-seed maintenance tools

## Seed content hash

`static/knowledge_graph_seed.json` stores its bundle content digest in
`manifest_sha256`. Despite the historical field name, this is **not** the hash
of the manifest file bytes.

`store_topics._read_knowledge_seed_bundle()` computes the value as follows:

1. Read every topic listed by `files`.
2. Normalize each topic with `_normalize_seed_topic()`.
3. Sort normalized topics by `id`.
4. Serialize this exact object as UTF-8 with `ensure_ascii=False`,
   `sort_keys=True`, and `separators=(",", ":")`:
   `{"protocol": seed_protocol_version, "revision": resolved_revision, "topics": normalized_topics}`.
   `resolved_revision` is `str(content_revision)`, falling back to `revision`,
   `version`, then `legacy`, exactly as `_read_knowledge_seed_bundle()` does.
5. Store the lowercase SHA-256 digest of those UTF-8 bytes as `manifest_sha256`.

The loader rejects a declared digest that differs from this canonical bundle
content. Recompute it whenever seed topics or their revision change; do not use
`sha256sum static/knowledge_graph_seed.json`.
