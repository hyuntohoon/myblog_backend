"""Small KMS envelope helpers shared by member token custody flows."""

import base64

from app.core.config import settings


def kms_encrypt_b64(plaintext: str) -> str:
    """KMS-encrypt UTF-8 plaintext and return its base64 envelope."""
    import boto3

    kms = boto3.client("kms", region_name=settings.AWS_DEFAULT_REGION)
    blob = kms.encrypt(
        KeyId=settings.USER_TOKENS_KMS_KEY_ID,
        Plaintext=plaintext.encode("utf-8"),
    )["CiphertextBlob"]
    return base64.b64encode(blob).decode("ascii")


def kms_decrypt_b64(ciphertext_b64: str) -> str:
    """Base64-decode and KMS-decrypt a UTF-8 envelope."""
    import boto3

    blob = base64.b64decode(ciphertext_b64, validate=True)
    kms = boto3.client("kms", region_name=settings.AWS_DEFAULT_REGION)
    plaintext = kms.decrypt(CiphertextBlob=blob)["Plaintext"]
    return plaintext.decode("utf-8")
