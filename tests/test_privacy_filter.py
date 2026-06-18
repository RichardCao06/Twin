"""Scenarios 5 & 6: single-chat hard filter, redaction, taint propagation.

Scenario 5: only conversationType=='group' AND conversationId in allowed_groups
becomes a Signal; single chats and non-allowed groups are dropped.

Scenario 6: redaction catches sk-/password=/phone-number style secrets and PII;
tainted content is not external-safe, so taint propagation blocks outbound use.
"""
from __future__ import annotations

from dws_agent.privacy.redaction import redact
from dws_agent.privacy.single_chat import classify_message, to_signal
from dws_agent.privacy.taint import is_external_safe, propagate

ALLOWED = {"cidGROUP_OK"}


# --- Scenario 5: single-chat hard filter ------------------------------------ #
def test_single_chat_message_is_dropped():
    msg = {
        "conversationType": "1",  # 1:1 single chat
        "conversationId": "cidGROUP_OK",
        "text": {"content": "hi"},
    }
    assert classify_message(msg, ALLOWED).kind == "drop"
    assert to_signal(msg, ALLOWED) is None


def test_group_in_allowlist_becomes_signal():
    msg = {
        "conversationType": "group",
        "conversationId": "cidGROUP_OK",
        "senderId": "u1",
        "text": {"content": "please review the doc"},
    }
    assert classify_message(msg, ALLOWED).kind == "signal"
    sig = to_signal(msg, ALLOWED)
    assert sig is not None
    assert sig.conversation_id == "cidGROUP_OK"
    # Group content is at least INTERNAL.
    assert sig.taint in ("INTERNAL", "SENSITIVE")


def test_group_not_in_allowlist_is_dropped():
    msg = {
        "conversationType": "group",
        "conversationId": "cidNOT_ALLOWED",
        "text": {"content": "secret plan"},
    }
    verdict = classify_message(msg, ALLOWED)
    assert verdict.kind == "drop"
    assert verdict.reason == "group_not_in_allowed_groups"
    assert to_signal(msg, ALLOWED) is None


def test_missing_fields_default_deny_drop():
    assert classify_message({}, ALLOWED).kind == "drop"
    assert classify_message({"conversationType": "group"}, ALLOWED).kind == "drop"


# --- Scenario 6: redaction hits + taint blocks outbound --------------------- #
def test_redacts_sk_high_entropy_token():
    text = "here is my key sk-abcdEFGH1234567890ZZ9988qwerVBNM token"
    res = redact(text)
    assert "sk-abcdEFGH1234567890ZZ9988qwerVBNM" not in res.text
    assert res.max_taint == "SENSITIVE"
    assert res.hits


def test_redacts_password_assignment():
    text = "db password=Sup3rSecretP@ssw0rdValue123 connect"
    res = redact(text)
    # The secret value should be redacted (high-entropy or connection detector).
    assert "Sup3rSecretP@ssw0rdValue123" not in res.text
    assert res.max_taint == "SENSITIVE"


def test_redacts_private_key_block():
    text = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEAabc123\n"
        "-----END RSA PRIVATE KEY-----"
    )
    res = redact(text)
    assert "MIIEpAIBAAKCAQEAabc123" not in res.text
    assert "[REDACTED:PRIVATE_KEY]" in res.text
    assert res.max_taint == "SENSITIVE"


def test_redacts_chinese_mobile_number():
    for text in [
        "call me at 13812345678 today",
        "phone 138-1234-5678 please",
        "reach +86 13912345678 anytime",
    ]:
        res = redact(text)
        assert "[REDACTED:PHONE]" in res.text, text
        assert res.max_taint in ("INTERNAL", "SENSITIVE"), text


def test_email_is_internal_taint():
    res = redact("contact alice@example.com for details")
    assert "alice@example.com" not in res.text
    assert res.max_taint == "INTERNAL"


def test_taint_propagation_blocks_outbound():
    """Tainted content is never external-safe; propagation keeps the strictest."""
    res = redact("token sk-abcdEFGH1234567890ZZ9988qwerVBNM here")
    derived = propagate([res.max_taint], own="CLEAN")
    assert derived == "SENSITIVE"
    assert is_external_safe(derived) is False


def test_clean_text_is_external_safe():
    res = redact("let us meet at noon for lunch")
    assert res.max_taint == "CLEAN"
    assert is_external_safe(propagate([res.max_taint], own="CLEAN")) is True


def test_sensitive_never_washes_down():
    # Even mixing with CLEAN inputs, SENSITIVE survives (no wash-down).
    assert propagate(["CLEAN", "INTERNAL", "SENSITIVE"], own="CLEAN") == "SENSITIVE"
