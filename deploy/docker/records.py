"""Extract one bounded JSON observation from an operator job's output."""

import json
import sys
from uuid import UUID


def main():
    content = sys.stdin.buffer.read(8 * 1024**2 + 1)
    if len(content) > 8 * 1024**2:
        raise ValueError("Job observation exceeds its byte limit.")
    records = [
        json.loads(line)
        for line in content.decode().splitlines()
        if line.startswith("{")
    ]
    key = sys.argv[1]
    matches = [value[key] for value in records if key in value]
    if len(matches) != 1:
        raise ValueError("Expected exactly one matching job observation.")
    value = matches[0]
    if key == "backup_id" and str(UUID(value)) != value:
        raise ValueError("Backup observation has an invalid identity.")
    print(value)


if __name__ == "__main__":
    main()
