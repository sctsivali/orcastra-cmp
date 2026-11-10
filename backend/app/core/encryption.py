"""Fernet encryption for sensitive values (certificates, tokens, passwords) cached in Redis."""

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
    """Encrypts and decrypts sensitive values with Fernet (AES-128 in CBC mode)."""
    
    def __init__(self, encryption_key: Optional[str] = None):
        """Use ``encryption_key`` (a base64 Fernet key), or resolve one with _get_or_create_key."""
        if encryption_key:
            try:
                self.key = encryption_key.encode() if isinstance(encryption_key, str) else encryption_key
                self.fernet = Fernet(self.key)
                logger.info("Initialized encryption with provided key")
            except Exception as e:
                logger.error(f"Invalid encryption key provided: {e}")
                raise EncryptionError(f"Invalid encryption key: {str(e)}")
        else:
            # Key from Vault, the environment or SECRET_KEY.
            self.key = self._get_or_create_key()
            self.fernet = Fernet(self.key)
            logger.info("Initialized encryption with generated key")
    
    def _get_or_create_key(self) -> bytes:
        """Resolve the key from Vault, then REDIS_ENCRYPTION_KEY, then a derivation of SECRET_KEY.

        Raises EncryptionError when none of them is configured.
        """
        vault_key = self._try_get_key_from_vault()
        if vault_key:
            return vault_key
        
        env_key = os.environ.get('REDIS_ENCRYPTION_KEY')
        if env_key:
            try:
                key = env_key.encode() if isinstance(env_key, str) else env_key
                Fernet(key)  # Raises on an invalid key.
                logger.info("Using encryption key from environment")
                return key
            except Exception as e:
                logger.warning(f"Invalid REDIS_ENCRYPTION_KEY in environment: {e}")
        
        secret = os.environ.get('SECRET_KEY', '')
        if not secret:
            raise EncryptionError(
                "No encryption key configured. Set one of: "
                "VAULT_ENABLED=true (recommended), REDIS_ENCRYPTION_KEY, or SECRET_KEY. "
                "Generate SECRET_KEY with: openssl rand -hex 32"
            )
        
        # A per-deployment salt derived from SECRET_KEY avoids a fixed salt and stays
        # deterministic for the same SECRET_KEY.
        import hashlib
        salt = hashlib.sha256(f"orcastra-salt-{secret[:8]}".encode()).digest()[:16]
        
        # PBKDF2-HMAC-SHA256 with 600k iterations, the OWASP recommendation.
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=600000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(secret.encode()))
        
        logger.warning(
            "Derived encryption key from SECRET_KEY. "
            "For production, store key in Vault at secret/data/orcastra/encryption-key!"
        )
        return key
    
    def _try_get_key_from_vault(self) -> Optional[bytes]:
        """Fetch the encryption key from Vault, or None when Vault is off, unreachable or has none.

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
            
            # KV v2 nests the payload under data.data.
            if "data" in data and "data" in data["data"]:
                key_data = data["data"]["data"]
            else:
                key_data = data.get("data", {})
            
            key_str = key_data.get("key")
            if not key_str:
                logger.warning(f"No 'key' field in Vault path {key_path}")
                return None
            
            key = key_str.encode() if isinstance(key_str, str) else key_str
            Fernet(key)  # Raises on an invalid key.
            
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
            encrypted_bytes = base64.urlsafe_b64decode(encrypted_data.encode('utf-8'))
            
            decrypted = self.fernet.decrypt(encrypted_bytes)
            return decrypted.decode('utf-8')
        except InvalidToken:
            logger.error("Decryption failed: Invalid token or wrong key")
            raise EncryptionError("Failed to decrypt data: Invalid token or wrong encryption key")
        except Exception as e:
            logger.error(f"Decryption failed: {e}")
            raise EncryptionError(f"Failed to decrypt data: {str(e)}")
    
    def is_encrypted(self, data: str) -> bool:
        """True when ``data`` decrypts with this key."""
        if not data or not isinstance(data, str):
            return False
        
        try:
            encrypted_bytes = base64.urlsafe_b64decode(data.encode('utf-8'))
            self.fernet.decrypt(encrypted_bytes)
            return True
        except:
            return False
    
    def rotate_key(self, new_key: str) -> 'DataEncryption':
        """A new instance with ``new_key``, for re-encrypting data under a rotated key."""
        return DataEncryption(encryption_key=new_key)
    
    @staticmethod
    def generate_key() -> str:
        """Generate a new Fernet key."""
        key = Fernet.generate_key()
        return key.decode('utf-8')


_encryption_instance: Optional[DataEncryption] = None


def get_encryption() -> DataEncryption:
    """The process-wide DataEncryption, created on first use."""
    global _encryption_instance
    if _encryption_instance is None:
        _encryption_instance = DataEncryption()
    return _encryption_instance


def encrypt_sensitive_data(data: Any, field_name: str = "") -> Any:
    """Encrypt ``data`` when ``field_name`` looks sensitive; otherwise return it unchanged."""
    sensitive_patterns = [
        'password', 'token', 'secret', 'key', 'cert', 'certificate',
        'credential', 'auth', 'private', 'trust_password'
    ]
    
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
    """Decrypt ``data`` when ``field_name`` looks sensitive and the value is encrypted."""
    sensitive_patterns = [
        'password', 'token', 'secret', 'key', 'cert', 'certificate',
        'credential', 'auth', 'private', 'trust_password'
    ]
    
    is_sensitive = any(pattern in field_name.lower() for pattern in sensitive_patterns)
    
    if is_sensitive and isinstance(data, str) and data:
        try:
            encryption = get_encryption()
            if encryption.is_encrypted(data):
                return encryption.decrypt(data)
        except EncryptionError as e:
            logger.warning(f"Failed to decrypt field {field_name}: {e}")
            return data  # Return original if decryption fails
    
    return data
