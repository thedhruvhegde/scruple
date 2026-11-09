"""A throwaway PostgreSQL cluster for tests.

`SCRUPLE_TEST_POSTGRES_URL` (a superuser URL, e.g. a CI service container) wins.
Otherwise this finds `initdb` and `pg_ctl` (PATH, then /usr/lib/postgresql/*/bin),
initialises a cluster in a fresh temp dir, starts it on a free localhost port and
removes it afterwards. If the binaries are missing, `start()` returns None and the
postgres tests skip.

PostgreSQL refuses to run as root; under root the server runs as the `postgres`
system user via `runuser`, if that user exists.
"""

from __future__ import annotations

import os
import pwd
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

ENV_URL = "SCRUPLE_TEST_POSTGRES_URL"


@dataclass
class Cluster:
    url: str
    _dir: Path | None = None
    _pg_ctl: list[str] | None = None

    def stop(self) -> None:
        if self._dir is None or self._pg_ctl is None:
            return
        subprocess.run(
            [*self._pg_ctl, "stop", "-D", str(self._dir / "data"), "-m", "immediate"],
            capture_output=True,
            check=False,
        )
        shutil.rmtree(self._dir, ignore_errors=True)


def find_bin(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for d in sorted(Path("/usr/lib/postgresql").glob("*/bin"), reverse=True):
        if (d / name).exists():
            return str(d / name)
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def start() -> Cluster | None:
    if url := os.environ.get(ENV_URL):
        return Cluster(url=url)
    initdb, pg_ctl = find_bin("initdb"), find_bin("pg_ctl")
    if initdb is None or pg_ctl is None:
        return None
    prefix: list[str] = []
    owner: pwd.struct_passwd | None = None
    if os.geteuid() == 0:
        try:
            owner = pwd.getpwnam("postgres")
        except KeyError:
            return None
        runuser = shutil.which("runuser")
        if runuser is None:
            return None
        prefix = [runuser, "-u", "postgres", "--"]
    root = Path(tempfile.mkdtemp(prefix="scruple-pg-"))
    if owner is not None:
        os.chown(root, owner.pw_uid, owner.pw_gid)
    data = root / "data"
    port = _free_port()
    subprocess.run(
        [*prefix, initdb, "-D", str(data), "-U", "scruple", "-A", "trust", "-E", "UTF8"],
        capture_output=True,
        check=True,
    )
    opts = f"-p {port} -k {root} -c listen_addresses=127.0.0.1 -c fsync=off"
    subprocess.run(
        [*prefix, pg_ctl, "start", "-D", str(data), "-w", "-l", str(root / "log"), "-o", opts],
        capture_output=True,
        check=True,
    )
    return Cluster(
        url=f"postgresql://scruple@127.0.0.1:{port}/postgres", _dir=root, _pg_ctl=[*prefix, pg_ctl]
    )
