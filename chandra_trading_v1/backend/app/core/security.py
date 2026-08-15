from cryptography.fernet import Fernet
from .config import settings

def _fernet() -> Fernet:
    if not settings.credential_encryption_key:
        raise RuntimeError("CREDENTIAL_ENCRYPTION_KEY is not configured")
    return Fernet(settings.credential_encryption_key.encode())

def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()

def decrypt_secret(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode()
