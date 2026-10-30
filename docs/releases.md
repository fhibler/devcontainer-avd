# Manual releases and recovery

## Publish a version

Only [release.yml](../.github/workflows/release.yml)'s manual dispatch publishes.
Dispatch it on the default branch and select a source ref from that branch's
history; empty selects the default branch. The workflow resolves an immutable
commit, reads the Feature's semantic version from metadata, refuses an existing
exact version, reruns the complete read-only tests, and publishes the tested
archive using the official pinned Dev Container CLI.

The Feature namespace for this repository is
`ghcr.io/fhibler/devcontainer-avd/avd`. The workflow derives and lowercases the
repository identity rather than hardcoding its owner. The official tooling
creates semantic-version aliases, including the `:1` alias used in the
[usage guide](../README.md). Use the immutable digest printed by the workflow
when the Feature payload must be reproducible.

The workflow does not rewrite versions, commit generated documentation, create
tags, or use a PAT. Publishing alone has package-write permission and releases
are serialized. Verification runs against the selected commit on both native
architectures; a green result for a different commit is insufficient.

## Verify the published artifact

Configure the package's public visibility in GHCR settings; anonymous pull
verification is required. After publication, a separate read-only job checks
the immutable digest, complete OCI Feature metadata, anonymous access, and
extracted file content/modes against the tested payload. The major, minor, and
`latest` aliases must each match the highest stable exact version in their
respective series, including when publishing an older release. Consumer jobs
then install that digest and run smoke tests on native amd64 and arm64.

Reports retain the source commit, Feature version, registry digest and alias
state, dependency/source identities, architecture evidence, and verification
links. Check the workflow summary and uploaded artifacts when investigating a
release.

## Recover from a verification failure

Registry publishing is not transactional. If checks after publication fail, the
exact version and aliases may already exist. Retain the published digest and
workflow artifacts, inspect the alias state with `devcontainer features info`,
and investigate. Do not overwrite an exact version or assume a failed workflow
unpublished it.

For a package-visibility failure, make the existing package public in GHCR
settings, then rerun **failed jobs only**. The successful publishing job stays
complete. Use the same retry for transient read-only verification failures.
If the artifact itself needs correction, fix the implementation, increment the
Feature version in metadata, update documentation, and publish the new version
through the full release workflow.
