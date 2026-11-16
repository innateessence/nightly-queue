"""Wait for the supervisor's durable launch record before executing trusted argv."""

import os
import sys


def main():
    descriptor = int(sys.argv[1])
    ready = os.read(descriptor, 1)
    os.close(descriptor)
    if ready != b"1":
        return 1
    os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
