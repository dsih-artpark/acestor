import pytest

from acestor_web.auth.passwords import (
    WeakPasswordError,
    check_policy,
    hash_password,
    verify_password,
)


def test_hash_and_verify_roundtrip():
    hashed = hash_password("CorrectHorse!Battery9")
    assert hashed != "CorrectHorse!Battery9"
    assert hashed.startswith("$argon2id$") or hashed.startswith("$argon2")
    assert verify_password("CorrectHorse!Battery9", hashed) is True
    assert verify_password("wrong-password!!123", hashed) is False


def test_hash_uses_random_salt():
    a = hash_password("SamePlaintext!123")
    b = hash_password("SamePlaintext!123")
    assert a != b


@pytest.mark.parametrize(
    "plain",
    [
        "short1!",  # under 12
        "alllowercase123",  # no symbol
        "AllLettersHere!",  # no digit
        "12345678901234!",  # no letter
    ],
)
def test_policy_rejects_weak(plain):
    with pytest.raises(WeakPasswordError):
        check_policy(plain)


def test_policy_accepts_strong():
    check_policy("CorrectHorse!Battery9")
