"""document-service: original MQXLIFF files, parsing and document identity.

Two identifiers are kept apart on purpose:

``document_id``
    The logical source document: :meth:`MqxliffDocument.source_fingerprint`.
    Two files with the same fingerprinted source share it, whatever their
    targets say.

``original_id``
    The exact uploaded bytes: SHA-256 of the file. Every distinct upload is
    stored and addressable on its own; nothing ever picks "the latest" file
    for a document implicitly.

Only this service reads or writes original files.
"""

SERVICE_NAME = "document-service"
