"""Export the reviewed plan and adopt issues by stable, owner-authored markers."""

import argparse
import json
from pathlib import Path
import re

from .delivery import DeliveryError, DeliveryUnknown
from .journal import Journal


REPOSITORY = "innateessence/nightly-queue"
REPOSITORY_ID = 1353963957
OWNER_ID = 17239750


def marker(feature):
    if not isinstance(feature, str) or not re.fullmatch(r"0[0-9]|1[0-5]", feature):
        raise ValueError("Select a known two-digit plan feature.")
    return f"<!-- vibearr-plan-feature: {feature} -->"


def export_plan(plan):
    features = []
    for row in re.findall(
        r"^\| (\d{2}) \| (P[012]) \| `([^`]+)` \| ([^|]+) \|", plan, re.M
    ):
        feature, priority, branch, dependencies = row
        match = re.search(
            rf"^### {feature} — ([^\n]+)\n(.*?)(?=^### |^## |\Z)", plan, re.M | re.S
        )
        if match is None:
            raise ValueError(f"Missing plan section {feature}.")
        depends = re.findall(r"\d{2}", dependencies)
        features.append(
            {
                "feature": feature,
                "title": f"vibearr {feature}: {match[1]}",
                "branch": branch,
                "depends_on": depends,
                "priority": {"P0": "high", "P1": "normal", "P2": "low"}[priority],
                "criteria": match[2].strip(),
            }
        )
    if len(features) != 16 or {value["feature"] for value in features} != {
        f"{i:02}" for i in range(16)
    }:
        raise ValueError("The reviewed seed requires all16 unique feature sections.")
    return {
        "schema_version": 1,
        "repository": REPOSITORY,
        "repository_id": REPOSITORY_ID,
        "owner_id": OWNER_ID,
        "features": features,
    }


def issue_body(feature, numbers):
    dependencies = [numbers[value] for value in feature["depends_on"]]
    body = f'{marker(feature["feature"])}\n\nPlan feature: {feature["feature"]}\nBranch: {feature["branch"]}\n'
    if dependencies:
        body += "Depends on: " + ", ".join(f"#{value}" for value in dependencies) + "\n"
    return (
        body
        + "\nAcceptance criteria and test expectations:\n\n"
        + feature["criteria"]
        + f"\n\n[Development plan](https://github.com/{REPOSITORY}/blob/main/docs/autonomous-ai-queue-plan.md) · [MVP contract](https://github.com/{REPOSITORY}/blob/main/docs/vibearr-mvp.md)\n"
    )


def existing_issues(github):
    result = []
    for page in range(1, 101):
        issues = github.request(
            "GET", f"/repos/{REPOSITORY}/issues?state=all&per_page=100&page={page}"
        )
        if not isinstance(issues, list):
            raise DeliveryError("Provider issue listing is invalid.")
        result.extend(issue for issue in issues if "pull_request" not in issue)
        if len(issues) < 100:
            return result
    raise DeliveryError(
        "Backlog exceeds the bounded100-page scan; reconcile explicitly."
    )


def discover(features, issues):
    numbers = {}
    for feature in features:
        key = feature["feature"]
        matches = [
            issue
            for issue in issues
            if issue.get("user", {}).get("id") == OWNER_ID
            and marker(key) in (issue.get("body") or "")
        ]
        if len(matches) > 1:
            raise DeliveryError(
                f"Multiple owner-authored issues carry feature {key}; reconcile duplicates explicitly."
            )
        if matches:
            number = matches[0].get("number")
            if type(number) is not int or number < 1:
                raise DeliveryError("Provider returned an invalid issue number.")
            numbers[key] = number
    if len(set(numbers.values())) != len(numbers):
        raise DeliveryError(
            "An owner issue carries multiple feature markers; each plan feature needs its own issue."
        )
    return numbers


def publish(seed, github, directory):
    repository, _ = github.repository(repository_id=REPOSITORY_ID)
    if repository["repository"] != REPOSITORY or repository["owner_id"] != OWNER_ID:
        raise DeliveryError(
            "The stable self repository has moved; reconcile the trusted seed explicitly."
        )
    with Journal(directory) as journal:
        state = journal.load()
        saved = state.setdefault(
            "bootstrap_backlog",
            {"repository_id": REPOSITORY_ID, "pending": {}, "numbers": {}},
        )
        if saved["repository_id"] != REPOSITORY_ID:
            raise DeliveryError(
                "Seed publication journal belongs to another repository."
            )
        numbers = discover(seed["features"], existing_issues(github))
        for key, number in saved["numbers"].items():
            if numbers.get(key) != number:
                raise DeliveryError(
                    "Previously adopted issue marker changed; reconcile before publication."
                )
        pending = list(seed["features"])
        while pending:
            ready = [
                feature
                for feature in pending
                if set(feature["depends_on"]) <= set(numbers)
            ]
            if not ready:
                raise DeliveryError("Seed prerequisites cannot be resolved.")
            for feature in ready:
                key = feature["feature"]
                if key not in numbers:
                    if key in saved["pending"]:
                        raise DeliveryUnknown(
                            f"Feature {key} create response is uncertain; retain its intent and reconcile the owner marker before retrying."
                        )
                    saved["pending"][key] = {"marker": marker(key)}
                    journal.save(state)
                    payload = {
                        "title": feature["title"],
                        "body": issue_body(feature, numbers),
                    }
                    if feature["priority"] != "normal":
                        payload["labels"] = [f'vibearr:priority-{feature["priority"]}']
                    try:
                        issue = github.request(
                            "POST", f"/repos/{REPOSITORY}/issues", payload
                        )
                    except DeliveryUnknown:
                        raise
                    except DeliveryError:
                        saved["pending"].pop(key, None)
                        journal.save(state)
                        raise
                    created = discover([feature], [issue])
                    if key not in created:
                        raise DeliveryUnknown(
                            "Created issue marker/author is unavailable; reconcile the saved intent."
                        )
                    numbers[key] = created[key]
                saved["numbers"][key] = numbers[key]
                saved["pending"].pop(key, None)
                journal.save(state)
                pending.remove(feature)
        return {"schema_version": 1, "repository_id": REPOSITORY_ID, "issues": numbers}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    content = (
        json.dumps(export_plan(arguments.plan.read_text()), indent=2, sort_keys=True)
        + "\n"
    )
    if arguments.output:
        with arguments.output.open("x") as stream:
            stream.write(content)
    else:
        print(content, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
