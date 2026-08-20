"""The shared driver: drive the agent, answer its dialogs, read what it left.

Every benchmark in the battery reduces to those three moves, so they live here
once rather than in four adapters. What differs between benchmarks is who
supplies the task and who scores the result — never the wire.

**Standard library only.** The driver is baked into the same isolated task
container as Second Brain and should not make each trial install another
dependency layer.

The wire it speaks is documented in the kernel repo's ``docs/HTTP_PROTOCOL.md``.
This is deliberately not a plugin: a client is debuggable from outside the
sandbox, and every benchmark run then dogfoods the same surface a real web
client uses.
"""

from driver.wire import Client, Frames

__all__ = ["Client", "Frames"]
