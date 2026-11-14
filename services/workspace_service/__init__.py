"""workspace-service: saved per-document work in PostgreSQL.

A workspace is keyed by ``document_id`` (document-service's source
fingerprint), because segment keys (``<trans-unit id>#<ordinal>``) are only
unique within one document. The per-document logic is the existing
:class:`memoq_harness.state.ProjectState` (``bind_segments``,
``capture_edits`` and the v5 per-document snapshot), run in memory for one
document per request; the snapshot is stored as JSONB instead of state.json.

This service never reads document files or calls other services.
"""

SERVICE_NAME = "workspace-service"
