"""Remove the task UID's persistent IPC objects between jobs."""

import ctypes
import os
from pathlib import Path


def main():
    os.setgroups([])
    os.setgid(1000)
    os.setuid(1000)
    libc = ctypes.CDLL(None, use_errno=True)
    for kind, identity in (("shm", "shmid"), ("sem", "semid"), ("msg", "msqid")):
        lines = (Path("/proc/sysvipc") / kind).read_text().splitlines()
        columns = lines[0].split()
        for line in lines[1:]:
            values = dict(zip(columns, line.split()))
            if values["uid"] != "1000":
                continue
            number = int(values[identity])
            if kind == "sem":
                result = libc.semctl(number, 0, 0)
            else:
                result = getattr(libc, kind + "ctl")(number, 0, None)
            if result != 0:
                raise OSError(ctypes.get_errno(), "Cannot reset task IPC objects.")


if __name__ == "__main__":
    main()
