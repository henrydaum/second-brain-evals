"""Host-side orchestration: containers, trials, aggregation.

The split against ``driver/`` is not organisational. ``driver/`` runs *inside*
a container and is standard-library only, because it also has to run inside
Harbor's and Boundary-Bench's task images. This package runs on the host,
talks to Docker, and is free to use whatever it likes.

Nothing here is imported by a benchmark adapter's in-container half. If an
adapter finds itself reaching into ``harness`` from inside a container, the
split has been crossed and something is about to work only on this machine.
"""
