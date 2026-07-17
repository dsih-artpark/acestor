from passlib.context import CryptContext


class WeakPasswordError(ValueError):
    pass


_pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


def hash_password(plain: str) -> str:
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd_context.verify(plain, hashed)


def check_policy(plain: str) -> None:
    if len(plain) < 12:
        raise WeakPasswordError("password too short")
    if not any(c.isalpha() for c in plain):
        raise WeakPasswordError("password requires a letter")
    if not any(c.isdigit() for c in plain):
        raise WeakPasswordError("password requires a digit")
    if not any(not c.isalnum() for c in plain):
        raise WeakPasswordError("password requires a non-alphanumeric character")
