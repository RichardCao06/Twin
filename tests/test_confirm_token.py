"""Scenario 4: confirm_token issue/verify semantics.

* A token issued over the normalized argv verifies with the same argv.
* Presenting a DIFFERENT (tampered) argv => argv-hash mismatch => REJECT.
* --yes / -y is ignored: a token issued without --yes verifies with --yes.
* Expired tokens (now > issued_at + ttl) => REJECT.
* One-time use: a verified token cannot be replayed.
"""
from __future__ import annotations

from dws_agent.policy import confirm
from dws_agent.policy.classifier import normalize_argv

ACTION = "AI-20260618-aabbccdd"


def _issue(home, full_argv, ttl=300, now=1000):
    norm = normalize_argv(full_argv)
    return confirm.issue_token(ACTION, norm, ttl, home, now=now)


def test_correct_argv_verifies(home):
    full = ["dws", "chat", "message", "send", "--to", "boss", "--text", "hi"]
    _issue(home, full)
    vr = confirm.verify_token(ACTION, full, home, now=1100)
    assert vr.ok is True


def test_tampered_argv_hash_mismatch_rejected(home):
    _issue(home, ["dws", "chat", "message", "send", "--to", "boss", "--text", "hi"])
    # Change the text => different normalized argv => hash mismatch.
    vr = confirm.verify_token(
        ACTION, ["dws", "chat", "message", "send", "--to", "boss", "--text", "HACKED"],
        home, now=1100,
    )
    assert vr.ok is False
    assert "hash mismatch" in vr.reason.lower()


def test_yes_flag_ignored_on_verify(home):
    """Token issued without --yes verifies when --yes is present (it's stripped)."""
    _issue(home, ["dws", "chat", "message", "send", "--text", "x"])
    vr = confirm.verify_token(
        ACTION, ["dws", "chat", "message", "send", "--text", "x", "--yes"], home, now=1100
    )
    assert vr.ok is True


def test_expired_token_rejected(home):
    _issue(home, ["dws", "chat", "message", "send", "--text", "x"], ttl=300, now=1000)
    # now beyond issued_at + ttl
    vr = confirm.verify_token(ACTION, ["dws", "chat", "message", "send", "--text", "x"],
                              home, now=1000 + 301)
    assert vr.ok is False
    assert "expired" in vr.reason.lower()


def test_token_is_one_time_use(home):
    _issue(home, ["dws", "chat", "message", "send", "--text", "x"], now=1000)
    first = confirm.verify_token(ACTION, ["dws", "chat", "message", "send", "--text", "x"],
                                 home, now=1100)
    assert first.ok is True
    replay = confirm.verify_token(ACTION, ["dws", "chat", "message", "send", "--text", "x"],
                                  home, now=1100)
    assert replay.ok is False
    assert "used" in replay.reason.lower()


def test_missing_record_rejected(home):
    vr = confirm.verify_token("AI-20260618-00000000",
                              ["dws", "chat", "message", "send"], home, now=1100)
    assert vr.ok is False
