"""
Encryption utilities for sensitive data in cache.
Uses Fernet (symmetric encryption) for encrypting sensitive data like certificates,
tokens, and passwords before storing in Redis.
"""

import logging
import os
import base64
from typing import Optional, Any
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)


class EncryptionError(Exception):
    """Exception raised for encryption/decryption errors."""
    pass


class DataEncryption:
    """
    Handles encryption and decryption of sensitive data.
    Uses Fernet (symmetric encryption with AES 128 in CBC mode).
    """
    
    def __init__(self, encryption_key: Optional[str] = None):
        """
        Initialize encryption handler.
        
        Args:
            encryption_key: Base64-encoded encryption key. If None, generates from environment
                          or creates a new one.
        """
        if encryption_key:
            try:
                self.key = encryption_key.encode() if isinstance(encryption_key, str) else encryption_key
                self.fernet = Fernet(self.key)
                logger.info("Initialized encryption with provided key")
            except Exception as e:
                logger.error(f"Invalid encryption key provided: {e}")
                raise EncryptionError(f"Invalid encryption key: {str(e)}")
        else:
            # Generate key from environment or create new one
            self.key = self._get_or_create_key()
            self.fernet = Fernet(self.key)
            logger.info("Initialized encryption with generated key")
    
    def _get_or_create_key(self) -> bytes:
        """
        Get encryption key from Vault, environment, or generate a new one.
        
        Priority order:
        1. Vault (most secure - secrets management)
        2. Environment variable REDIS_ENCRYPTION_KEY (acceptable for dev)
        3. Derive from SECRET_KEY (fallback only)
        """
        # Priority 1: Try to get from Vault first
        vault_key = self._try_get_key_from_vault()
        if vault_key:
            return vault_key
        
        # Priority 2: Try to get from environment
        env_key = os.environ.get('REDIS_ENCRYPTION_KEY')
        if env_key:
            try:
                # Validate it's a valid Fernet key
                key = env_key.encode() if isinstance(env_key, str) else env_key
                Fernet(key)  # Test if valid
                logger.info("Using encryption key from environment")
                return key
            except Exception as e:
                logger.warning(f"Invalid REDIS_ENCRYPTION_KEY in environment: {e}")
        
        # Priority 3: Derive key from a secret or generate new one (FALLBACK ONLY)
        secret = os.environ.get('SECRET_KEY', 'orcastra-dashboard-default-secret-change-in-production')
        
        # Use PBKDF2HMAC to derive a key from the secret
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b'orcastra-salt-v1',  # Fixed salt for deterministic key generation
            iterations=100000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(secret.encode()))
        
        logger.warning(
            "Generated encryption key from SECRET_KEY. "
            "For production, store key in Vault at secret/data/orcastra/encryption-key!"
        )
        return key
    
    def _try_get_key_from_vault(self) -> Optional[bytes]:
        """
        Try to fetch encryption key from Vault.

        Expected Vault path: secret/data/orcastra/encryption-key
        Expected data format: {"key": "<fernet-base64-key>"}
        """
        try:
            import requests
            
            vault_addr = os.environ.get('VAULT_ADDR')
            vault_token = os.environ.get('VAULT_TOKEN')
            vault_enabled = os.environ.get('VAULT_ENABLED', 'false').lower() == 'true'
            
            if not vault_enabled or not vault_addr or not vault_token:
                logger.debug("Vault not configured for encryption key fetch")
                return None
            
            # Get custom path or use default
            key_path = os.environ.get('VAULT_ENCRYPTION_KEY_PATH', 'secret/data/orcastra/encryption-key')
            url = f"{vault_addr.rstrip('/')}/v1/{key_path}"
            
            response = requests.get(
                url,
                headers={"X-Vault-Token": vault_token},
                timeout=(2, 3)
            )
            
            if response.status_code == 404:
                logger.debug(f"Encryption key not found in Vault at {key_path}")
                return None
            
            response.raise_for_status()
            data = response.json()
            
            # Handle KV v2 format
            if "data" in data and "data" in data["data"]:
                key_data = data["data"]["data"]
            else:
                key_data = data.get("data", {})
            
            key_str = key_data.get("key")
            if not key_str:
                logger.warning(f"No 'key' field in Vault path {key_path}")
                return None
            
            # Validate it's a valid Fernet key
            key = key_str.encode() if isinstance(key_str, str) else key_str
            Fernet(key)  # Test if valid
            
            logger.info(f"Using encryption key from Vault ({key_path})")
            return key
            
        except Exception as e:
            logger.debug(f"Could not fetch encryption key from Vault: {e}")
            return None
    
    def encrypt(self, data: str) -> str:
        if not data:
            return data
        
        try:
            if isinstance(data, str):
                data = data.encode('utf-8')
            
            encrypted = self.fernet.encrypt(data)
            return base64.urlsafe_b64encode(encrypted).decode('utf-8')
        except Exception as e:
            logger.error(f"Encryption failed: {e}")
            raise EncryptionError(f"Failed to encrypt data: {str(e)}")
    
    def decrypt(self, encrypted_data: str) -> str:
        if not encrypted_data:
            return encrypted_data
        
        try:
            # Decode from base64
            encrypted_bytes = base64.urlsafe_b64decode(encrypted_data.encode('utf-8'))
            
            # Decrypt
            decrypted = self.fernet.decrypt(encrypted_bytes)
            return decrypted.decode('utf-8')
        except InvalidToken:
            logger.error("Decryption failed: Invalid token or wrong key")
            raise EncryptionError("Failed to decrypt data: Invalid token or wrong encryption key")
        except Exception as e:
            logger.error(f"Decryption failed: {e}")
            raise EncryptionError(f"Failed to decrypt data: {str(e)}")
    
    def is_encrypted(self, data: str) -> bool:
        """
        Check if data appears to be encrypted.
        """
        if not data or not isinstance(data, str):
            return False
        
        try:
            # Try to decode as base64 and decrypt
            encrypted_bytes = base64.urlsafe_b64decode(data.encode('utf-8'))
            self.fernet.decrypt(encrypted_bytes)
            return True
        except:
            return False
    
    def rotate_key(self, new_key: str) -> 'DataEncryption':
        """
        Create new encryption instance with rotated key.
        Use this to re-encrypt data with a new key.
        """
        return DataEncryption(encryption_key=new_key)
    
    @staticmethod
    def generate_key() -> str:
        """
        Generate a new Fernet key.
        """
        key = Fernet.generate_key()
        return key.decode('utf-8')


# Global encryption instance
_encryption_instance: Optional[DataEncryption] = None


def get_encryption() -> DataEncryption:
    """
    Get or create global encryption instance.
    """
    global _encryption_instance
    if _encryption_instance is None:
        _encryption_instance = DataEncryption()
    return _encryption_instance


def encrypt_sensitive_data(data: Any, field_name: str = "") -> Any:
    """
    Encrypt sensitive data if it's a sensitive field.
    """
    # Define sensitive field patterns
    sensitive_patterns = [
        'password', 'token', 'secret', 'key', 'cert', 'certificate',
        'credential', 'auth', 'private', 'trust_password'
    ]
    
    # Check if field is sensitive
    is_sensitive = any(pattern in field_name.lower() for pattern in sensitive_patterns)
    
    if is_sensitive and isinstance(data, str) and data:
        try:
            encryption = get_encryption()
            return encryption.encrypt(data)
        except EncryptionError as e:
            logger.error(f"Failed to encrypt sensitive field {field_name}: {e}")
            return data  # Return original if encryption fails
    
    return data


def decrypt_sensitive_data(data: Any, field_name: str = "") -> Any:
    """
    Decrypt sensitive data if it's encrypted.
    """
    # Define sensitive field patterns
    sensitive_patterns = [
        'password', 'token', 'secret', 'key', 'cert', 'certificate',
        'credential', 'auth', 'private', 'trust_password'
    ]
    
    # Check if field is sensitive
    is_sensitive = any(pattern in field_name.lower() for pattern in sensitive_patterns)
    
    if is_sensitive and isinstance(data, str) and data:
        try:
            encryption = get_encryption()
            # Try to decrypt - if it fails, might not be encrypted
            if encryption.is_encrypted(data):
                return encryption.decrypt(data)
        except EncryptionError as e:
            logger.warning(f"Failed to decrypt field {field_name}: {e}")
            return data  # Return original if decryption fails
    
    return data
