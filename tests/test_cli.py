"""CLI integration: init / confirm / status (in-band confirm flow).

* init scaffolds the home and seeds policy.yaml.
* confirm classifies + issues + verifies a confirm_token for a write; refuses
  never-listed commands; treats R0 as nothing-to-confirm.
* --yes is never consulted by confirm classification.
"""
from __future__ import annotations

from pathlib import Path

from dws_agent.cli.main import main


def test_init_scaffolds_home(home, capsys):
    rc = main(["init", "--force"])
    assert rc == 0
    assert Path(home.policy_file).exists()
    assert Path(home.audit_dir).exists()


def test_confirm_issues_and_verifies_write(home, capsys):
    rc = main(["confirm", "--action-id", "AI-20260618-11111111",
               "--argv", "dws", "chat", "message", "send", "--text", "hi"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "CONFIRMED" in out
    assert "confirm_token=" in out


def test_confirm_refuses_never_command(home, capsys):
    rc = main(["confirm", "--action-id", "AI-20260618-22222222",
               "--argv", "dws", "auth", "export"])
    err = capsys.readouterr().err
    assert rc == 4
    assert "never permitted" in err.lower()


def test_confirm_r0_nothing_to_confirm(home, capsys):
    rc = main(["confirm", "--action-id", "AI-20260618-33333333",
               "--argv", "dws", "chat", "message", "list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no confirmation required" in out.lower()


def test_confirm_ignores_yes_flag(home, capsys):
    """--yes present must not turn a write into an auto-allow."""
    rc = main(["confirm", "--action-id", "AI-20260618-44444444",
               "--argv", "dws", "chat", "message", "send", "--text", "x", "--yes"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "CONFIRMED" in out  # still required confirmation despite --yes


def test_status_runs(home, capsys):
    main(["init", "--force"])
    rc = main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "DWS_AGENT_HOME" in out
    assert "refresh-guard lock" in out
