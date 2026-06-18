"""Scenarios 2 & 3: deterministic PolicyGate classification.

* send/approve/reject (and any write) WITHOUT --yes are default-denied to
  HUMAN_CONFIRM (need a confirm_token); --yes is never consulted.
* R0 whitelist commands are AUTO.
* unknown subcommands fall through to default-deny (>= R2 => HUMAN_CONFIRM).
* never-listed auth commands are terminal DENY (not confirmable).
"""
from __future__ import annotations

import pytest

from conftest import make_intent

from dws_agent.policy.gate import PolicyGate


@pytest.fixture
def gate(home):
    return PolicyGate(paths=home)


# --- Scenario 3: R0 whitelist auto-allows reads ----------------------------- #
@pytest.mark.parametrize(
    "argv",
    [
        ["dws", "chat", "message", "list"],
        ["dws", "chat", "message", "list", "--conv", "g1"],
        ["dws", "contact", "search", "--name", "x"],
        ["dws", "report", "list"],
        ["dws", "minutes", "list"],
        ["dws", "calendar", "event", "get", "--id", "e1"],
    ],
)
def test_r0_whitelist_is_auto(gate, argv):
    d = gate.evaluate(make_intent(argv))
    assert d.level == "R0"
    assert d.decision == "AUTO"
    assert d.requires_confirm is False
    assert d.never is False


# --- Scenario 2: writes need confirm, --yes is ignored ---------------------- #
@pytest.mark.parametrize(
    "argv",
    [
        ["dws", "chat", "message", "send", "--to", "boss", "--text", "hi"],  # explicit R2 rule
        ["dws", "oa", "approval", "reject", "--id", "a1"],            # unknown => default-deny
        ["dws", "oa", "approval", "approve", "--id", "a1"],           # unknown => default-deny
    ],
)
def test_write_needs_confirm(gate, argv):
    d = gate.evaluate(make_intent(argv))
    assert d.decision == "HUMAN_CONFIRM"
    assert d.requires_confirm is True
    assert d.never is False
    assert d.level in ("R1", "R2", "R3")


def test_yes_flag_does_not_change_classification(gate):
    """--yes / -y must NEVER make a command more permissive."""
    base = gate.evaluate(make_intent(["dws", "chat", "message", "send", "--text", "x"]))
    with_yes = gate.evaluate(make_intent(["dws", "chat", "message", "send", "--text", "x", "--yes"]))
    with_y = gate.evaluate(make_intent(["dws", "-y", "chat", "message", "send", "--text", "x"]))
    assert base.decision == with_yes.decision == with_y.decision == "HUMAN_CONFIRM"
    # The normalized-argv hash is identical whether or not --yes is present.
    assert base.argv_norm_sha256 == with_yes.argv_norm_sha256 == with_y.argv_norm_sha256


def test_approval_create_is_r1(gate):
    d = gate.evaluate(make_intent(["dws", "todo", "task", "create", "--form", "f1"]))
    assert d.level == "R1"
    assert d.decision == "HUMAN_CONFIRM"


# --- Scenario 3: unknown subcommand => default-deny R2 ---------------------- #
def test_unknown_subcommand_default_deny_r2(gate):
    d = gate.evaluate(make_intent(["dws", "frobnicate", "the-widget"]))
    assert d.level == "R2"
    assert d.decision == "HUMAN_CONFIRM"
    assert d.requires_confirm is True


def test_unknown_top_level_default_deny(gate):
    d = gate.evaluate(make_intent(["dws", "totally-unknown-cmd"]))
    assert d.level == "R2"
    assert d.decision == "HUMAN_CONFIRM"


# --- never-list is terminal DENY -------------------------------------------- #
@pytest.mark.parametrize(
    "argv",
    [
        ["dws", "auth", "export"],
        ["dws", "auth", "import"],
        ["dws", "auth", "logout"],
        ["dws", "auth", "reset"],
    ],
)
def test_never_list_is_terminal_deny(gate, argv):
    d = gate.evaluate(make_intent(argv))
    assert d.decision == "DENY"
    assert d.never is True
    assert d.requires_confirm is False


def test_auth_other_is_r3(gate):
    """auth subcommands not in never still hit the auth=>R3 rule."""
    d = gate.evaluate(make_intent(["dws", "auth", "status"]))
    assert d.level == "R3"
    assert d.decision == "HUMAN_CONFIRM"
    assert d.human_only is True


# --- argv hard reject -------------------------------------------------------- #
def test_missing_argv_hard_reject(gate):
    intent = make_intent(["dws", "chat", "message", "list"])
    intent["argv"] = []
    d = gate.evaluate(intent)
    assert d.decision == "DENY"


def test_argv0_not_dws_hard_reject(gate):
    intent = make_intent(["rm", "-rf", "/"])
    d = gate.evaluate(intent)
    assert d.decision == "DENY"


# --- semantic labels may only tighten, never relax -------------------------- #
def test_clean_labels_keep_r0_auto(gate):
    """Conservative-default / CLEAN labels do NOT tighten an R0 read off AUTO."""
    d = gate.evaluate(make_intent(["dws", "chat", "message", "list"], taint="CLEAN",
                                  commit_class="none"))
    assert d.decision == "AUTO"


def test_sensitive_label_only_tightens_never_relaxes(gate):
    """AND-strictness (取严): a SENSITIVE label can only tighten. It pushes an
    R0 read up to HUMAN_CONFIRM and can NEVER move anything toward AUTO."""
    auto = gate.evaluate(make_intent(["dws", "chat", "message", "list"], taint="INTERNAL"))
    assert auto.decision == "AUTO"
    tightened = gate.evaluate(make_intent(["dws", "chat", "message", "list"], taint="SENSITIVE"))
    assert tightened.decision == "HUMAN_CONFIRM"  # stricter, not relaxed


def test_sensitive_label_keeps_write_confirm(gate):
    d = gate.evaluate(
        make_intent(["dws", "chat", "message", "send", "--text", "x"], taint="SENSITIVE",
                    commit_class="yes")
    )
    assert d.decision == "HUMAN_CONFIRM"
    assert d.requires_confirm is True
