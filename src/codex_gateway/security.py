import hashlib
import hmac
import secrets

def hash_api_key(raw_key: str, pepper: str) -> str:
    return hmac.new(pepper.encode(), raw_key.encode(), hashlib.sha256).hexdigest()

def keys_equal(raw_key: str, expected_hash: str, pepper: str) -> bool:
    return hmac.compare_digest(hash_api_key(raw_key, pepper), expected_hash)

def generate_api_key() -> tuple[str, str]:
    prefix = secrets.token_hex(4)
    return f"cag_{prefix}_{secrets.token_urlsafe(32)}", prefix
