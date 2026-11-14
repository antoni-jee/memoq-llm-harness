-- document-service, migration 0002: exported MQXLIFF files.
--
-- {schema} is replaced by the validated, quoted schema name at run time.
-- Keep statements plain (see 0001): no functions, DO blocks or dollar quoting.
-- export_id is the SHA-256 of the exported bytes; warnings hold per-segment
-- tag-integrity results (segment keys and placeholder numbers only, no text).

CREATE TABLE {schema}.exports (
    export_id text PRIMARY KEY CHECK (export_id ~ '^[0-9a-f]{64}$'),
    original_id text NOT NULL REFERENCES {schema}.originals (original_id),
    filename text NOT NULL,
    size_bytes bigint NOT NULL CHECK (size_bytes > 0),
    segments_written integer NOT NULL CHECK (segments_written > 0),
    tag_source text NOT NULL CHECK (tag_source IN ('target', 'source')),
    mark_confirmed boolean NOT NULL,
    warnings jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX exports_original_id_idx ON {schema}.exports (original_id, created_at);
