import hashlib
import hmac

def sign_hmac_sha256(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()

def verify_hmac_sha256(secret: str, message: bytes, provided_signature: str) -> bool:
    return hmac.compare_digest(sign_hmac_sha256(secret, message), provided_signature)
