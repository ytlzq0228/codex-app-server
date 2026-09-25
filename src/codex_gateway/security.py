import hashlib
import hmac
import secrets

PASSWORD_ITERATIONS = 600_000

def hash_api_key(raw_key: str, pepper: str) -> str:
    return hmac.new(pepper.encode(), raw_key.encode(), hashlib.sha256).hexdigest()

def keys_equal(raw_key: str, expected_hash: str, pepper: str) -> bool:
    return hmac.compare_digest(hash_api_key(raw_key, pepper), expected_hash)

def generate_api_key() -> tuple[str, str]:
    prefix = secrets.token_hex(4)
    return f"cag_{prefix}_{secrets.token_urlsafe(32)}", prefix


def hash_password(password: str, *, iterations: int = PASSWORD_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, raw_iterations, salt_hex, expected_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(raw_iterations))
        return hmac.compare_digest(digest.hex(), expected_hex)
    except (ValueError, TypeError):
        return False
