"""Typed shape of the Nextcloud connector's ``connector_specific_config``.

Field names, types and defaults MUST match ``NextcloudConnector.__init__``
exactly; ``test_connector_config_models`` enforces this.
Credentials (server URL, username, app password) are NOT here - they live in
the encrypted Credential row and arrive via ``load_credentials``.
"""

from onyx.configs.app_configs import INDEX_BATCH_SIZE
from onyx.connectors.connector_config import ConnectorConfig


class NextcloudConnectorConfig(ConnectorConfig):
    # Folders relative to the WebDAV user root, e.g. ["Documents", "Projects/Notes"].
    # Empty means the whole account (every folder under the WebDAV root).
    folders: list[str] = []
    verify_ssl: bool = True
    max_file_size_mb: int = 25
    batch_size: int = INDEX_BATCH_SIZE
