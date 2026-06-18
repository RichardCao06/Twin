"""dws-agent CLI subpackage.

Console entrypoints:
- ``dws-agent`` -> :func:`dws_agent.cli.main.main`
- ``dwsd``      -> :func:`dws_agent.cli.dwsd.main`

This package contains no business logic of its own; it orchestrates the
``core``, ``policy``, ``store`` and ``executor`` modules via their documented
contracts. All sibling imports are performed lazily inside functions so this
package imports cleanly even when a sibling module is not yet present.
"""
