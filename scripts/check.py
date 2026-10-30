#!/usr/bin/env python3
"""Offline schema, documentation, workflow, and example consistency checks."""

import json
import re
import sys
from pathlib import Path

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parents[1]


def generated_options():
    feature = json.loads((ROOT / "src/avd/devcontainer-feature.json").read_text())
    rows = ["| Option | Type | Default | Description |", "| --- | --- | --- | --- |"]
    for name, value in feature["options"].items():
        default = str(value["default"]).lower() if isinstance(value["default"], bool) else value["default"]
        description = value["description"]
        if "enum" in value:
            description += " Allowed values: " + ", ".join(f"`{choice}`" for choice in value["enum"]) + "."
        rows.append(f"| `{name}` | {value['type']} | `{default}` | {description} |")
    return "\n".join(rows)


def generated_images():
    return "\n".join(f"- `{image}`" for image in json.loads((ROOT / "test/images.json").read_text()))


def docs(update):
    for path, marker, value in [
        (ROOT / "README.md", "options", generated_options()),
        (ROOT / "docs/platforms.md", "images", generated_images()),
    ]:
        text = path.read_text()
        start, end = f"<!-- {marker}:start -->", f"<!-- {marker}:end -->"
        assert text.count(start) == text.count(end) == 1 and text.index(start) < text.index(end), (
            f"Missing or invalid generation markers in {path}"
        )
        expected = re.sub(f"{re.escape(start)}.*?{re.escape(end)}", f"{start}\n{value}\n{end}", text, flags=re.S)
        if update:
            path.write_text(expected)
        else:
            assert text == expected, f"Stale {marker} reference; run python scripts/check.py --update"


def main():
    feature = json.loads((ROOT / "src/avd/devcontainer-feature.json").read_text())
    jsonschema.validate(feature, json.loads((ROOT / "test/support/feature-schema.json").read_text()))
    assert re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", feature["version"])
    for reference in (ROOT / "README.md", ROOT / "src/avd/README.md"):
        assert feature["version"] in reference.read_text(), f"Stale Feature version in {reference}"
    tool_versions = dict(
        line.split("=", 1)
        for line in (ROOT / "scripts/versions.env").read_text().splitlines()
        if line and not line.startswith("#")
    )
    assert "@devcontainers/cli@" + tool_versions["DEVCONTAINER_CLI"] in (ROOT / "docs/development.md").read_text(), (
        "Stale documented CLI version"
    )
    examples = {path.parent.parent.name for path in ROOT.glob("examples/*/.devcontainer/devcontainer.json")}
    exercised = {scenario.get("example") for scenario in json.loads((ROOT / "test/scenarios.json").read_text())}
    assert examples <= exercised, "Every runnable example must have a real lifecycle scenario"
    docs("--update" in sys.argv)
    for path in [*ROOT.glob(".github/workflows/*.yml"), *ROOT.glob("test/avd/**/*.yml")]:
        yaml.safe_load(path.read_text())
    for path in ROOT.glob("examples/*/.devcontainer/devcontainer.json"):
        example = json.loads(path.read_text())
        assert "image" in example and "build" not in example
        for name in example["features"]:
            if name.startswith("."):
                assert (path.parent / name / "install.sh").is_file(), (path, name)
    for path in [
        ROOT / "README.md",
        ROOT / "src/avd/README.md",
        *ROOT.glob("docs/**/*.md"),
        *ROOT.glob("examples/*/README.md"),
    ]:
        for target in re.findall(r"\]\(([^)#]+)(?:#[^)]*)?\)", path.read_text()):
            if not re.match(r"[a-z]+://", target):
                assert (path.parent / target).exists(), (path, target)
    workflows = {path.name: path.read_text() for path in ROOT.glob(".github/workflows/*.yml")}
    test_workflow = yaml.load(workflows["test.yml"], Loader=yaml.BaseLoader)
    assert set(test_workflow["on"]) == {"push", "pull_request", "workflow_dispatch", "schedule", "workflow_call"}
    assert not test_workflow["on"]["push"] and not test_workflow["on"]["pull_request"], (
        "Per-commit verification must have no filters"
    )
    assert test_workflow["permissions"] == {"contents": "read"}
    required = test_workflow["jobs"]["required"]
    assert set(required["needs"]) == set(test_workflow["jobs"]) - {"required"}
    assert required["if"] == "always()"
    release_workflow = yaml.load(workflows["release.yml"], Loader=yaml.BaseLoader)
    assert set(release_workflow["on"]) == {"workflow_dispatch"}, "Only manual dispatch may publish"
    assert release_workflow["permissions"] == {"contents": "read"}
    assert release_workflow["jobs"]["publish"]["permissions"] == {"contents": "read", "packages": "write"}
    assert release_workflow["jobs"]["verify"]["permissions"] == {"contents": "read"}
    assert release_workflow["jobs"]["published"]["permissions"] == {"contents": "read", "packages": "read"}
    for name, job in release_workflow["jobs"].items():
        if name != "publish":
            assert job.get("permissions", {}).get("packages") != "write"
    assert release_workflow["concurrency"]["cancel-in-progress"] == "false"
    for text in workflows.values():
        for action in re.findall(r"uses: ([^\s]+)", text):
            assert action.startswith("./") or re.search(r"@[a-f0-9]{40}$", action), f"Unpinned action {action}"
        assert "continue-on-error" not in text
    print("Schema, documentation, example, and workflow consistency checks passed")


if __name__ == "__main__":
    main()
