"""Remote backend clients for the memoQ harness.

Used by the Streamlit app only in remote mode (``HARNESS_BACKEND=remote``);
local mode is the default and is unaffected. No application wiring lives here
yet - this slice provides the config and the HTTP clients only.
"""

from __future__ import annotations

from .client import (
    BaseClient,
    DocumentClient,
    ProcessingClient,
    RemoteClients,
    RemoteError,
    WorkspaceClient,
    build_clients,
)
from .view import (
    BatchProgress,
    RemoteViewRecord,
    RemoteWorkspaceView,
)
from .config import (
    ENV_BACKEND,
    ENV_DOCUMENT_URL,
    ENV_HTTP_TIMEOUT,
    ENV_PROCESSING_URL,
    ENV_PROCESS_TIMEOUT,
    ENV_WORKSPACE_URL,
    ConfigError,
    RemoteConfig,
    load_config,
)

__all__ = [
    "ConfigError",
    "RemoteConfig",
    "load_config",
    "ENV_BACKEND",
    "ENV_DOCUMENT_URL",
    "ENV_WORKSPACE_URL",
    "ENV_PROCESSING_URL",
    "ENV_HTTP_TIMEOUT",
    "ENV_PROCESS_TIMEOUT",
    "RemoteError",
    "BaseClient",
    "DocumentClient",
    "WorkspaceClient",
    "ProcessingClient",
    "RemoteClients",
    "build_clients",
    "RemoteWorkspaceView",
    "RemoteViewRecord",
    "BatchProgress",
]
