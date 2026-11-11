"""RDK X5 / fnOS laboratory tooling with an offline-safe default.

The simulator and the ``fake`` backend set never write a real disk, switch real
power or open a serial port.  Real CP5/serial/flash backends exist behind an
explicit environment gate, root, a plan-bound token, the shared board lock and
``--confirm-board``; see ``README.md`` and
``fnos/docs/hardware-backends-20261106.md``.
"""

__version__ = "0.1.0"

SCHEMA_PREFIX = "fnos-lab"
