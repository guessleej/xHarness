from xharness.redact import mask, scrub, scrub_mapping


def test_mask_keeps_a_recognisable_stub():
    masked = mask("sk-live-abcdefghijklmnop")
    assert masked.startswith("sk-l") and masked.endswith("op")
    assert "abcdefghijkl" not in masked


def test_short_values_are_fully_masked():
    assert mask("hunter2") == "*******"


def test_empty_is_empty():
    assert mask("") == "" and mask(None) == ""


def test_bearer_header_scrubbed():
    text = "failed: Authorization: Bearer abcdef1234567890xyz"
    assert "abcdef1234567890xyz" not in scrub(text)
    assert "Bearer" in scrub(text)


def test_query_string_secret_scrubbed():
    text = "GET https://api.example/v1/chat?api_key=SUPERSECRETVALUE failed"
    out = scrub(text)
    assert "SUPERSECRETVALUE" not in out and "api_key=" in out


def test_provider_key_shapes_scrubbed():
    for secret in ("sk-abcdefghijklmnop", "hf_abcdefghijklmnop", "ghp_abcdefghijklmnop"):
        assert secret not in scrub(f"token was {secret} here")


def test_url_password_scrubbed():
    assert "hunter2xyz" not in scrub("postgres://user:hunter2xyz@db:5432/x")


def test_ordinary_text_untouched():
    text = "the model returned 42 tokens in 1.2s"
    assert scrub(text) == text


def test_mapping_masks_by_key_name():
    out = scrub_mapping({"user": "alice", "password": "a-long-password", "nested": {"api_key": "sk-abcdefghijk"}})
    assert out["user"] == "alice"
    assert "a-long-password" not in str(out)
    assert "abcdefghijk" not in str(out)


def test_mapping_scrubs_values_too():
    out = scrub_mapping({"detail": "called https://x/v1?token=ABCDEFGHIJKLMN"})
    assert "ABCDEFGHIJKLMN" not in out["detail"]


def test_mapping_survives_deep_nesting():
    data = {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"token": "x"}}}}}}}}
    assert scrub_mapping(data)  # must not recurse forever
