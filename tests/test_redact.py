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


def test_modern_provider_keys_are_masked_whole():
    from xharness.redact import scrub

    for key in ("sk-proj-abcdefghijklmnopqrstuvwxyz0123456789",
                "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv_wxyz-0123456789"):
        out = scrub(f"failed with {key} today")
        assert key[12:] not in out and out.startswith("failed with ") and out.endswith(" today")


def test_env_style_assignments_are_masked():
    from xharness.redact import scrub

    out = scrub('OPENAI_API_KEY=abcdef123456789 DB_PASSWORD="hunter2hunter2"')
    assert "abcdef123456789" not in out and "hunter2hunter2" not in out
    assert out.startswith("OPENAI_API_KEY=")


def test_prose_with_token_counts_is_left_alone():
    from xharness.redact import scrub

    text = "max_tokens=16384 and total_tokens: 5918, sk-learn is a word"
    assert scrub(text) == text
