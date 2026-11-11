"""Read-only fnOS ARM input audit.

The package is intentionally self-contained under ``fnos/integration/input_audit``.
It never mounts an image, never executes image content and never uses sudo.
"""

from .audit import Auditor, SCHEMA, TOOL_VERSION

__all__ = ["Auditor", "SCHEMA", "TOOL_VERSION"]
