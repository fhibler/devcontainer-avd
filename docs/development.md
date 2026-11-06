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
request, manual request, and reusable call. It validates and
packages once, then tests each platform image on both native architectures.
The runner and test-group matrix axes expand independently, so every group runs
on both amd64 and arm64.
Known image-tag aliases share a job so BuildKit can reuse the install layer
when tags resolve to the same digest. Every tag is still resolved and smoke
checked. Image pulls get up to three attempts for transient network/server errors,
with a three-minute timeout per attempt. A platform build is capped at 20
minutes per image. DNF and microdnf discard cached metadata between up to three
install attempts, covering empty or malformed `repomd.xml` downloads. Bootstrap
downloads also retry connection failures. Metadata downloads, Python package
installs, Galaxy collection installs, and Git fetches get three total attempts
for transient connection errors, truncated responses, rate limits, and server
errors, with two- and four-second backoffs. Package installs and Git downloads
have a five-minute timeout per attempt; Galaxy and Python HTTP requests have
60-second timeouts in their respective clients. Authentication, checksum, and dependency-conflict failures
are not retried. APT and bootstrap downloads use their native download retries.
Job names
show the architecture and image tags; each group retains image-pull and build
logs alongside its other artifacts. Smoke-test failures are not retried.

Additional integration jobs cover modes, optional dependencies, Python
selections, users/UID adjustment, official Feature composition, duplicate
installation, source lifecycle, and runnable examples on both architectures.
Scenarios using the same image run in pairs on one runner so they can reuse its
Docker image cache; failures remain reported per scenario.
The pinned AVD 6.4.0 regression fixture runs alongside current-release and
source-`devel` coverage; preparation resolves moving source refs to commits.
Platform and integration builds import and export BuildKit layer caches through
GitHub Actions. Caches are isolated by runner architecture, job type, and test
group. A missing cache follows the normal installation path. Successful runs
on the default branch save updated caches; pull requests only restore them.
Each successful export replaces its previous local contents to avoid retaining
obsolete blobs. GitHub's cache quota and eviction policy still apply.

Preparation resolves the current AVD release and moving source refs on every
run. Only caches matching those identities, tool pins, and a daily UTC freshness
value are imported. Feature contents, options, Python selections, and immutable
base-image digests also participate in BuildKit's layer keys. This allows reuse
on reruns while refreshing unpinned transitive dependencies daily. Integration
tests retain their image-based configurations when caching is enabled.
Image pulls, lifecycle hooks, and offline smoke checks still run on cache hits.

Before the test matrix starts, six native warmup jobs prepare download bundles:
Python 3.12, 3.13, and 3.14 on each architecture. They derive dependency cases from
the scenario definitions, including optional collections and SSH transports,
the fixed AVD release, exact Python patch fixture, and both source commits.
Warmups install on UBI9 (glibc 2.34), matching the oldest supported libc, so
cached interpreters and binary wheels also work on the newer supported images.
Installed virtual environments are never shared across images.

The bundles contain the verified UV bootstrap archive, compressed standalone
Python distributions, UV's raw package cache, current metadata, and complete
Galaxy collection download sets. Galaxy resolutions are scoped to requirements
and the exact Ansible controller version. Metadata and collection resolutions
expire daily; wheel downloads and interpreters persist between runs through
architecture/minor-specific Actions caches. Warmup upgrades floating Python
minors, prunes unused interpreter archives, and prunes unreferenced UV cache entries. Only
successful default-branch jobs save persistent caches; pull requests restore them.

Pinned source snapshots, including Git history and tags, are prepared once and
cached separately by commit identity. Within the test matrix, only source
scenario groups download them.
Each test group downloads just the Python minors its scenarios use. Compressed
archives preserve Linux filenames, permissions, and internal links; checksums
are verified before use. Downloads live outside diagnostic artifact directories.
Integration jobs add a temporary local seed Feature before the production AVD
Feature, preserving image-based configurations and normal lifecycle hooks.
The seed uses `/var/tmp` for temporary files, avoiding Podman's permission changes
on `/tmp` when committing Feature bind mounts.
Source hooks copy package inputs into the development user's writable cache.
Each environment still performs its own package installation, editable source
builds, and verification. Editable PyAVD is explicitly rebuilt so cached wheels
cannot bypass schema/template compilation in a fresh checkout.
Missing or expired entries use the normal retrying
download path; corrupt inputs fail verification.
The UBI9 minimal platform check also recreates its runtime in a network-disabled
snapshot, proving bootstrap, interpreter, wheel, and Galaxy reuse. Its OS
prerequisites are already installed; ordinary platform builds still exercise
real OS package transactions.

To exercise preparation locally after `ci.py prepare` (using a native container
engine), run:

```sh
python3 scripts/downloads.py sources
python3 scripts/downloads.py warm 3.14
python3 scripts/downloads.py merge
AVD_CI_DOWNLOADS="$PWD/tmp/downloads/seed" python3 scripts/ci.py platform ubuntu:26.10
```

`AVD_DOWNLOAD_WORK` selects the workspace for raw caches, bundles, and merged
inputs; it defaults to the ignored `tmp/downloads` directory. Integration source
cases also need a warmup for their Python minor. Ordinary local tests without
`AVD_CI_DOWNLOADS` continue to exercise uncached installation. A complete
integration `up` has a 30-minute limit; the platform build limit remains 20 minutes.

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
