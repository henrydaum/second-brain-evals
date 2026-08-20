"""One container per trial, created and destroyed around a single task.

**Fresh per trial is the whole design.** The template exists so that every
trial starts from a byte-identical DATA_DIR: the entrypoint copies
``/opt/sb-template`` into a pristine ``/data/Second Brain`` on first start, so
a new container is a new machine. Reusing one across trials would let a
standing grant, a conversation, a config edit or a stray file leak from one
trial into the next -- and every one of those leaks makes later trials easier
in a way that no result file would record.

Files move with ``docker cp`` rather than a bind mount, for two reasons. Bind
mounts on Windows are slow and are already documented as a source of flaky
timing in the implementation guide, and a guest-mode adapter -- where Harbor
owns the container -- cannot mount anything at all. One path for both modes is
worth a couple of copies per trial.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid

#: Docker Desktop's binary. The same constant ``build_template.py`` uses, and
#: overridable for a machine that puts it somewhere else.
DOCKER = os.environ.get(
    "SB_DOCKER", r"C:\Program Files\Docker\Docker\resources\bin\docker.exe")

IMAGE = os.environ.get("SB_BENCH_IMAGE", "secondbrain:bench")

#: Where the driver was baked. See ``Dockerfile.bench``.
DRIVER_ENTRY = "/opt/sb-driver/driver/run_task.py"

#: The task tree inside the container. Also what ``SB_WRITABLE_DIRS`` names,
#: so the agent can write deliverables there without a dialog per file.
WORKDIR = "/work/task"


class DockerError(RuntimeError):
    """A docker invocation failed. Carries what was run and what came back."""


def docker(*args, capture=True, timeout=600, check=True):
    result = subprocess.run([DOCKER, *args], text=True, timeout=timeout,
                            capture_output=capture, check=False)
    if check and result.returncode != 0:
        raise DockerError("docker " + " ".join(args) + "\n"
                          + (result.stderr or result.stdout or "")[:2000])
    return result


class Trial:
    """A container that exists for exactly one task attempt.

    Used as a context manager, so a failure anywhere still removes it. A
    benchmark that leaves containers behind runs out of disk somewhere around
    the third full sweep, and the ones it leaves are the failures -- which are
    also the ones somebody wants to inspect. Hence ``keep``.
    """

    def __init__(self, name=None, image=IMAGE, env_file=None, env=None,
                 keep=False, extra_args=()):
        self.name = name or ("sb-trial-" + uuid.uuid4().hex[:12])
        self.image = image
        self.env_file = env_file
        self.env = dict(env or {})
        self.keep = keep
        self.extra_args = list(extra_args)
        self.started_at = None

    # ------------------------------------------------------------- lifecycle

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.destroy()
        return False

    def start(self):
        args = ["run", "-d", "--name", self.name]
        if self.env_file:
            args += ["--env-file", str(self.env_file)]
        for key, value in self.env.items():
            args += ["-e", str(key) + "=" + str(value)]
        args += self.extra_args
        args.append(self.image)
        docker(*args)
        self.started_at = time.time()
        return self

    def destroy(self):
        if self.keep:
            return
        docker("rm", "-f", self.name, check=False)

    # ------------------------------------------------------------------ i/o

    def put(self, host_path, container_path):
        """Copy a file or a directory's *contents* into the container."""
        host_path = str(host_path)
        if os.path.isdir(host_path):
            self.exec_(["mkdir", "-p", container_path], user="0")
            docker("cp", os.path.join(host_path, "."),
                   self.name + ":" + container_path)
        else:
            docker("cp", host_path, self.name + ":" + container_path)

    def get(self, container_path, host_path):
        """Copy a path out. Missing is not an error -- the agent may not
        have produced it, which is a result rather than a harness failure."""
        os.makedirs(os.path.dirname(str(host_path)) or ".", exist_ok=True)
        result = docker("cp", self.name + ":" + container_path,
                        str(host_path), check=False)
        return result.returncode == 0

    def exec_(self, argv, user=None, env=None, timeout=3600, capture=True):
        args = ["exec"]
        if user is not None:
            args += ["-u", str(user)]
        for key, value in (env or {}).items():
            args += ["-e", str(key) + "=" + str(value)]
        args += [self.name, *argv]
        return docker(*args, capture=capture, timeout=timeout, check=False)

    def logs(self, tail=200):
        return docker("logs", "--tail", str(tail), self.name,
                      check=False).stdout

    # ----------------------------------------------------------------- task

    def prepare(self, spec, fixtures=None, workdir=WORKDIR):
        """Place the spec and the task's starting files.

        The directory is created as root and handed to the unprivileged user
        the app runs as. ``docker cp`` into a path that does not exist fails,
        and a directory the agent cannot write is indistinguishable, from the
        model's side, from a task it is not allowed to do.
        """
        self.exec_(["mkdir", "-p", workdir], user="0")
        if fixtures and os.path.isdir(str(fixtures)):
            self.put(fixtures, workdir)
        payload = json.dumps(spec, ensure_ascii=False)
        self.exec_(["sh", "-c", "cat > " + workdir + "/task.json <<'SBSPEC'\n"
                    + payload + "\nSBSPEC"], user="0")
        self.exec_(["chown", "-R", "1000:1000", "/work"], user="0")

    def drive(self, workdir=WORKDIR, timeout=3600):
        """Run the driver inside the container and answer with its output."""
        return self.exec_(
            ["python", DRIVER_ENTRY, workdir + "/task.json"],
            timeout=timeout)

    def collect(self, dest, workdir=WORKDIR):
        """Bring the whole task tree home, bundle and deliverables together."""
        dest = str(dest)
        if os.path.isdir(dest):
            shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        return self.get(workdir, dest)
