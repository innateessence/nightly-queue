"""Bind an inspected browser/app image pair to exported exact candidate source."""

import argparse
import json
import sys

from vibearr_runner.browser_receipts import validate_binding


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--application-image", required=True)
    parser.add_argument("--browser-image", required=True)
    options = parser.parse_args()
    content = sys.stdin.buffer.read(8193)
    if len(content) > 8192:
        raise ValueError("Browser source observation is oversized.")
    source = json.loads(content)
    if source.get("kind") != "implementation-source":
        raise ValueError("Use the exact source exported for the application build.")
    value = {
        "schema_version": 1,
        "revision": source["commit_sha"],
        "tree_sha": source["tree_sha"],
        "source_digest": source["source_digest"],
        "application_image": options.application_image,
        "browser_image": options.browser_image,
    }
    print(json.dumps(validate_binding(value)))


if __name__ == "__main__":
    main()
