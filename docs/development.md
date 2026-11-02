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
packages once, then tests each platform image on both native architectures.
The runner and test-group matrix axes expand independently, so every group runs
on both amd64 and arm64.
Known image-tag aliases share a job so BuildKit can reuse the install layer
when tags resolve to the same digest. Every tag is still resolved and smoke
checked. Image pulls get up to three attempts for transient network/server errors,
with a three-minute timeout per attempt. A platform build is capped at 20
minutes per image. DNF and microdnf discard cached metadata between up to three
install attempts, covering empty or malformed `repomd.xml` downloads. Bootstrap
downloads also retry connection failures. Job names
show the architecture and image tags; each group retains image-pull and build
logs alongside its other artifacts. Smoke-test failures are not retried.

Additional integration jobs cover modes, optional dependencies, Python
selections, users/UID adjustment, official Feature composition, duplicate
installation, source lifecycle, and runnable examples on both architectures.
Scenarios using the same image run in pairs on one runner so they can reuse its
Docker image cache; failures remain reported per scenario.
The pinned AVD 6.4.0 regression fixture runs alongside current-release and
source-`devel` coverage; preparation resolves moving source refs to commits.
Workflow runs start on fresh runners; cache reuse is limited to repeated images
within the same test job.

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
