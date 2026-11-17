import os
import sys

from .environment import load


def main():
    load()
    if len(sys.argv) < 2:
        raise SystemExit("Supply a container command.")
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
