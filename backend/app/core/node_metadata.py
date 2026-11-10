"""Per-node attributes (display name, hypervisor type) kept in a JSON file, with a memory fallback."""

import json
import logging
from typing import Dict, Optional
from pathlib import Path
import threading

logger = logging.getLogger(__name__)


class NodeMetadataStore:
    """Process-wide singleton over the metadata file."""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if not hasattr(self, '_initialized'):
            self.metadata_file = Path("/app/config/node_metadata.json")
            self._memory_cache: Optional[Dict[str, Dict[str, str]]] = None
            self._ensure_metadata_file()
            self._initialized = True
    
    def _ensure_metadata_file(self) -> None:
        """Create the file and its directory when missing; on failure, fall back to memory."""
        try:
            self.metadata_file.parent.mkdir(parents=True, exist_ok=True)
            if not self.metadata_file.exists():
                self._write_metadata({})
                logger.info(f"Created metadata file: {self.metadata_file}")
            else:
                # Pre-load the memory cache.
                self._read_metadata()
        except Exception as e:
            logger.warning(
                f"Could not initialize metadata file '{self.metadata_file}': {e}. "
                "Will use in-memory storage as fallback."
            )
            self._memory_cache = {}
    
    def _read_metadata(self) -> Dict[str, Dict[str, str]]:
        """Read metadata from file, falling back to in-memory cache."""
        try:
            with self._lock:
                if self.metadata_file.exists():
                    with open(self.metadata_file, 'r') as f:
                        data = json.load(f)
                        self._memory_cache = data
                        return data
                return self._memory_cache.copy() if self._memory_cache else {}
        except Exception as e:
            logger.warning(f"Error reading metadata file: {e}. Using in-memory cache.")
            return self._memory_cache.copy() if self._memory_cache else {}
    
    def _write_metadata(self, metadata: Dict[str, Dict[str, str]]) -> None:
        """Write metadata to the file; on failure, keep it in memory for this process."""
        try:
            with self._lock:
                with open(self.metadata_file, 'w') as f:
                    json.dump(metadata, f, indent=2)
        except PermissionError as e:
            logger.warning(
                f"Permission denied writing metadata file '{self.metadata_file}': {e}. "
                "Node metadata will be kept in memory only. "
                "Fix: ensure the config directory is writable by the app user."
            )
            self._memory_cache = metadata
        except Exception as e:
            logger.warning(f"Could not write metadata file: {e}. Using in-memory fallback.")
            self._memory_cache = metadata
    
    def get_node_display_name(self, node_name: str) -> Optional[str]:
        metadata = self._read_metadata()
        node_meta = metadata.get(node_name, {})
        return node_meta.get("display_name")
    
    def set_node_display_name(self, node_name: str, display_name: str) -> None:
        metadata = self._read_metadata()
        
        if node_name not in metadata:
            metadata[node_name] = {}
        
        metadata[node_name]["display_name"] = display_name
        self._write_metadata(metadata)
        logger.info(f"Updated display name for node '{node_name}' to '{display_name}'")
    
    def set_node_type(self, node_name: str, node_type: str) -> None:
        """Record the node's hypervisor type, lowercased; None removes it."""
        metadata = self._read_metadata()
        
        if node_name not in metadata:
            metadata[node_name] = {}
        
        if node_type is not None:
            metadata[node_name]["type"] = node_type.lower()
            logger.info(f"Updated type for node '{node_name}' to '{node_type}'")
        else:
            if "type" in metadata[node_name]:
                del metadata[node_name]["type"]
            logger.info(f"Removed type for node '{node_name}' (will be detected after authentication)")
        
        self._write_metadata(metadata)
    
    def get_all_node_metadata(self) -> Dict[str, Dict[str, str]]:
        return self._read_metadata()
    
    def delete_node_metadata(self, node_name: str) -> None:
        metadata = self._read_metadata()
        if node_name in metadata:
            del metadata[node_name]
            self._write_metadata(metadata)
            logger.info(f"Deleted metadata for node '{node_name}'")


_metadata_store: Optional[NodeMetadataStore] = None


def get_node_metadata_store() -> NodeMetadataStore:
    global _metadata_store
    if _metadata_store is None:
        _metadata_store = NodeMetadataStore()
    return _metadata_store
