#!/usr/bin/env python3
"""
Generate encryption key for Redis cache encryption.

Usage:
   `python generate_encryption_key.py`

This will generate a new Fernet encryption key that can be used in .env file:
    `REDIS_ENCRYPTION_KEY=<generated-key>`
"""

from cryptography.fernet import Fernet

def main():
    key = Fernet.generate_key()
    key_str = key.decode('utf-8')
    
    print("=" * 70)
    print("Redis Cache Encryption Key Generator")
    print("=" * 70)
    print()
    print("Generated Encryption Key:")
    print(key_str)
    print()
    print("Add this to your .env file:")
    print(f"REDIS_ENCRYPTION_KEY={key_str}")
    print()
    print("IMPORTANT SECURITY NOTES:")
    print("  1. Keep this key secret and secure")
    print("  2. Store it in a secure location (e.g., secrets manager)")
    print("  3. Do not commit it to version control")
    print("  4. If the key is lost, cached data cannot be decrypted")
    print("  5. Rotating the key will require clearing the cache")
    print()
    print("=" * 70)

if __name__ == "__main__":
    main()
