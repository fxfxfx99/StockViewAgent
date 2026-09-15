from app.security.passwords import hash_password, verify_password


def test_password_roundtrip():
    salt, hx = hash_password("secret-1")
    assert verify_password("secret-1", salt, hx)
    assert not verify_password("wrong", salt, hx)
