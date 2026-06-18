"""macOS launchd LaunchAgent plist generator for dwsd.

Renders a LaunchAgent plist that runs the ``dwsd`` daemon under launchd with
``KeepAlive`` (relaunch on crash), injecting ``DWS_AGENT_HOME`` into its
environment and directing stdout/stderr to ``$DWS_AGENT_HOME/logs``.

By default this module only *renders/prints* the plist. Writing to
``~/Library/LaunchAgents`` happens only when ``install_plist(..., write=True)``
is called, keeping the generator side-effect-free for tests and inspection.
"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

LAUNCHD_DIR = Path.home() / "Library" / "LaunchAgents"
DEFAULT_LABEL = "com.dws-agent.dwsd"


def _load_paths():
    try:
        from dws_agent.core import paths as core_paths  # type: ignore

        return core_paths.get_paths()
    except Exception:
        from dws_agent.cli.main import _FallbackPaths  # type: ignore

        return _FallbackPaths()


def render_plist(paths, dwsd_path: str, label: str = DEFAULT_LABEL) -> str:
    """Return the XML text of a LaunchAgent plist for dwsd.

    Parameters
    ----------
    paths:
        core.paths Paths object (provides ``home`` and ``logs_dir``).
    dwsd_path:
        absolute path to the ``dwsd`` executable (console script) to run.
    label:
        launchd job label (also the plist filename stem).

    The plist sets ``DWS_AGENT_HOME`` so the daemon resolves the same runtime
    root, enables ``KeepAlive`` + ``RunAtLoad``, and writes logs under
    ``$DWS_AGENT_HOME/logs``.
    """
    home = str(paths.home)
    logs = Path(paths.logs_dir)
    stdout_log = str(logs / "dwsd.out.log")
    stderr_log = str(logs / "dwsd.err.log")

    e = escape  # local alias
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{e(label)}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{e(dwsd_path)}</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>DWS_AGENT_HOME</key>
        <string>{e(home)}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ProcessType</key>
    <string>Background</string>
    <key>WorkingDirectory</key>
    <string>{e(home)}</string>
    <key>StandardOutPath</key>
    <string>{e(stdout_log)}</string>
    <key>StandardErrorPath</key>
    <string>{e(stderr_log)}</string>
</dict>
</plist>
"""


def install_plist(paths, *, write: bool = False) -> Path:
    """Render the dwsd plist and (optionally) write it to ~/Library/LaunchAgents.

    Returns the target plist path. When ``write`` is False (default), the plist
    XML is printed to stdout and the file is NOT created — callers can review it
    before installing. When ``write`` is True, the plist is written and the
    logs directory is ensured to exist.

    The ``dwsd`` executable is resolved from PATH; if not found, falls back to
    ``<home>/bin/dwsd`` as a placeholder so the rendered plist is still useful.
    """
    import shutil

    dwsd_path = shutil.which("dwsd") or str(Path(paths.home) / "bin" / "dwsd")
    target = LAUNCHD_DIR / (DEFAULT_LABEL + ".plist")
    xml = render_plist(paths, dwsd_path)

    if not write:
        print(xml)
        print("# (dry-run) would write to: %s" % target)
        return target

    LAUNCHD_DIR.mkdir(parents=True, exist_ok=True)
    Path(paths.logs_dir).mkdir(parents=True, exist_ok=True)
    target.write_text(xml, "utf-8")
    print("wrote launchd plist: %s" % target)
    print("load with: launchctl load -w %s" % target)
    return target


def main(argv=None) -> int:  # pragma: no cover - thin CLI wrapper
    """Optional standalone entry: ``python -m dws_agent.cli.launchd [--write]``."""
    import argparse
    import sys

    p = argparse.ArgumentParser(
        prog="dws-agent-launchd",
        description="generate the dwsd launchd LaunchAgent plist",
    )
    p.add_argument(
        "--write",
        action="store_true",
        help="write the plist to ~/Library/LaunchAgents (default: print only)",
    )
    args = p.parse_args(sys.argv[1:] if argv is None else argv)
    install_plist(_load_paths(), write=args.write)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
