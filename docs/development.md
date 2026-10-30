# Development and testing

## Try the local Feature

Clone the repository and select an example. Examples use a local Feature path,
so they test your working tree:

```sh
git clone git@github.com:fhibler/devcontainer-avd.git
cd devcontainer-avd
npm install --global @devcontainers/cli@0.90.0
devcontainer up --workspace-folder examples/runtime
devcontainer exec --workspace-folder examples/runtime avd-info
```

You can also open an example folder in a Dev Containers editor. Each example
documents its workspace requirements:
[runtime](../examples/runtime/README.md), [tools](../examples/tools/README.md),
[workspace source](../examples/source-workspace/README.md), and
[Git source](../examples/source-git/README.md).

## Run checks and integration tests

Tool versions are maintained in [scripts/versions.env](../scripts/versions.env);
bootstrap pins and architecture checksums are in
[src/avd/bootstrap.env](../src/avd/bootstrap.env). With Python 3.12+ and Node 22:

```sh
bash scripts/install-ci-tools.sh
bash scripts/validate.sh
npm install --global @devcontainers/cli@0.90.0
python3 scripts/ci.py prepare
python3 scripts/ci.py platform ubuntu:26.10
python3 scripts/ci.py scenario source-workspace
```

The tool-install script targets Linux amd64, matching the validation CI runner.
On other development hosts, provide the pinned lint tools on `PATH` before
running validation. Container tests require Docker;
`CONTAINER_ENGINE=podman` selects a compatible local Podman service. Architecture
checks reject an emulated backend as evidence for the other architecture.
Preparation records `HEAD`; commit the intended source before preparing a
package for commit-level verification.

Pure tests use Python unittest. Integration tests execute the extracted archive,
real installations, offline configuration/Markdown generation, and real
`devcontainer up` hooks. Source tests run contributor checks and live template
edits in disposable checkouts. They do not modify consumers' checkouts.
The vendored Feature schema comes from the
[Dev Container specification](https://github.com/devcontainers/spec/blob/main/schemas/devContainerFeature.schema.json).

## GitHub Actions

The [test workflow](../.github/workflows/test.yml) runs on every push, pull
request, weekly schedule, manual request, and reusable call. It validates and
packages once, then runs a separate visible job for each OS image/native
architecture pair in the [platform matrix](platforms.md). Each job has its own
status, logs, and artifacts; installation and smoke phases run inside its
`Test native platform` step.

Additional integration jobs cover modes, optional dependencies, Python
selections, users/UID adjustment, official Feature composition, duplicate
installation, source lifecycle, and runnable examples on both architectures.
The pinned AVD 6.4.0 regression fixture runs alongside current-release and
source-`devel` coverage; preparation resolves moving source refs to commits.
Downloads and installations are cold; previous environments are not reused as
proof of installation.

`Required tests` is the stable branch-protection check. All required results,
including unexpectedly skipped or cancelled jobs, must pass. Normal CI has
read-only permissions and never publishes. See [release operations](releases.md).

## Maintain the contract and documentation

Extend supported AVD/Python families through tested Feature updates. Update
metadata, constraints, bootstrap checksums when needed, platform/scenario data,
examples, and compatibility notes together. Keep the pinned regression fixture
until deliberately retired.

After metadata or image-matrix changes, use the validation environment to
regenerate the README options and platform image list:

```sh
.tools/venv/bin/python scripts/check.py --update
bash scripts/validate.sh
```

Validation checks generated references, local documentation links, examples,
schemas, shell/Python formatting and lint, workflows, and Markdown, including
all files under `docs/`.
