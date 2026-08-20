"""The shared driver: drive the agent, answer its dialogs, read what it left.

Every benchmark in the battery reduces to those three moves, so they live here
once rather than in four adapters. What differs between benchmarks is who
supplies the task and who scores the result — never the wire.

**Standard library only.** Terminal-Bench and Boundary-Bench install the agent
into *their* task container, which is a minimal image where ``pip install`` is
either unavailable or a per-trial cost nobody wants to pay 445 times. This
package must run on a bare ``python3``. Anything needing a dependency belongs
in ``harness/``, which runs on the host and is unconstrained.

The wire it speaks is documented in the kernel repo's ``docs/HTTP_PROTOCOL.md``.
This is deliberately not a plugin: a client is debuggable from outside the
sandbox, and every benchmark run then dogfoods the same surface a real web
client uses.
"""

from driver.wire import Client, Frames

__all__ = ["Client", "Frames"]
