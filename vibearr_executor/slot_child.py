"""Drop all task authority before loading any candidate code."""

import ctypes
import errno
import os
import resource
import sys


def main():
    if os.getuid() != 0:
        raise SystemExit("Task initialization must run as the trusted worker user.")
    libc = ctypes.CDLL(None, use_errno=True)
    for capability in range(64):
        if (
            libc.prctl(24, capability, 0, 0, 0) != 0
            and ctypes.get_errno() != errno.EINVAL
        ):
            raise OSError(
                ctypes.get_errno(), "Cannot clear task capability bounding set."
            )
    if libc.prctl(38, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Cannot prohibit task privilege escalation.")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024, 32 * 1024 * 1024))
    os.setgroups([])
    os.setgid(1000)
    os.setuid(1000)
    os.chdir("/work")
    os.umask(0o077)
    os.execve(sys.argv[1], sys.argv[1:], dict(os.environ))


if __name__ == "__main__":
    main()
