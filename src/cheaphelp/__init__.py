"""cheaphelp package.

An AI software-engineer that triages GitHub issues, plans, implements, and reviews changes on your repos using cheap OpenRouter models via the opencode harness.
"""

from __future__ import annotations

from cheaphelp._internal.cli import get_parser, main

__all__: list[str] = ["get_parser", "main"]
