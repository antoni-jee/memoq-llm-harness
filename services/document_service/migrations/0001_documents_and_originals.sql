-- document-service, migration 0001: logical documents and exact originals.
--
-- {schema} is replaced by the validated, quoted schema name at run time.
-- Keep statements plain: the runner splits on ";" at the end of a line, so do
-- not use functions, DO blocks or dollar quoting in these files.

CREATE TABLE {schema}.documents (
    document_id text PRIMARY KEY CHECK (document_id ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE {schema}.originals (
    original_id text PRIMARY KEY CHECK (original_id ~ '^[0-9a-f]{64}$'),
    document_id text NOT NULL REFERENCES {schema}.documents (document_id),
    filename text NOT NULL,
    size_bytes bigint NOT NULL CHECK (size_bytes > 0),
    segment_count integer NOT NULL CHECK (segment_count >= 0),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX originals_document_id_idx ON {schema}.originals (document_id, created_at);
