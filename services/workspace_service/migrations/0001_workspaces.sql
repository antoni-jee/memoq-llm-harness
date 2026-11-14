-- workspace-service, migration 0001: one saved workspace per document.
--
-- {schema} is replaced by the validated, quoted schema name at run time.
-- Keep statements plain: the runner splits on ";" at the end of a line, so do
-- not use functions, DO blocks or dollar quoting in these files.
--
-- snapshot is the v5 per-document entry of memoq_harness.state.ProjectState
-- (what state.json keeps under documents[<id>]); state_version records that
-- format. revision increases by one on every effective change and is the
-- optimistic-concurrency token for saves. There is deliberately no foreign
-- key into the document-service schema: each service owns its own data.

CREATE TABLE {schema}.workspaces (
    document_id text PRIMARY KEY CHECK (document_id ~ '^[0-9a-f]{64}$'),
    snapshot jsonb NOT NULL,
    state_version integer NOT NULL,
    revision bigint NOT NULL CHECK (revision >= 1),
    tag_source text NOT NULL CHECK (tag_source IN ('target', 'source')),
    bound_original_id text CHECK (bound_original_id ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
