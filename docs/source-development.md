# AVD source development

Set `mode` to `source` to use editable PyAVD and the live Ansible collection from
the same AVD checkout. Dependencies come from that checkout's `dev`, `doc`, and
`coverage` groups, editable `python-avd[ansible]` with `editable_mode=compat`,
and `anta[cli]`. Galaxy dependencies come from its `galaxy.yml`.

## Use your workspace

Place this configuration in an AVD checkout's `.devcontainer/devcontainer.json`:

```json
{
  "name": "AVD source",
  "image": "mcr.microsoft.com/devcontainers/base:ubuntu-24.04",
  "remoteUser": "vscode",
  "features": {
    "ghcr.io/fhibler/devcontainer-avd/avd:1": {
      "mode": "source",
      "sourceType": "workspace",
      "sourcePath": "."
    }
  },
  "waitFor": "postCreateCommand"
}
```

`sourcePath` resolves from the lifecycle working directory after the workspace
mount exists; it can also be an absolute container path. The checkout must have
root `pyproject.toml` dependency groups, `python-avd/pyproject.toml`, and the real
`ansible_collections/arista/avd` tree with `galaxy.yml` and plugins. Setup
preserves its branch, remotes, and working changes. It does not check out
`sourceRef` in workspace mode.

The [local workspace example](../examples/source-workspace/README.md) shows how
to use this repository's Feature with an external AVD checkout.

## Clone a repository

Use Git source when your project workspace is separate from the AVD checkout:

```json
{
  "name": "AVD contributor",
  "image": "mcr.microsoft.com/devcontainers/base:ubuntu-24.04",
  "remoteUser": "vscode",
  "features": {
    "ghcr.io/fhibler/devcontainer-avd/avd:1": {
      "mode": "source",
      "sourceType": "git",
      "sourceRepository": "https://github.com/aristanetworks/avd.git",
      "sourceRef": "v6.4.0"
    }
  },
  "waitFor": "postCreateCommand"
}
```

Git clones into `~/.local/share/avd/source` as the configured user. `sourceRef`
accepts a branch, tag, or commit; its default is `devel`. For immutable
repeatability, use a full commit hash. The resolved commit is recorded. HTTPS
and SSH repositories are accepted; supply credentials through ordinary Git/SSH
facilities, never embedded Feature options.

A new clone is staged until the requested ref is checked out successfully.
Failed clone/fetch attempts leave no partial checkout, so setup can be retried.

Repeated setup reuses a matching origin and commit. If moving to the requested
ref would discard user changes, setup fails with instructions to commit/stash
or move the checkout aside. It never resets or cleans automatically. See the
[local Git example](../examples/source-git/README.md).

## Lifecycle, edits, and updates

The image build provisions Python and prepares source paths. The Feature's
`postCreateCommand` activates source after the workspace is mounted, before the
consumer's command. Set `waitFor` to `postCreateCommand` when automation must
wait for activation. Setup does not replace the container entrypoint or run on
every start, attach, or shell prompt.

The collection loads directly from the real checkout through a Feature-owned
`.pth` file. Verification checks editable PyAVD, the active collection, and
upstream source detection against their common checkout. Development versions
can differ in formatting between Python and Galaxy metadata.

Run `avd-setup-source` after dependency changes. For workspace source, also rerun
it after checking out another ref. It rereads dependency inputs, reconciles the
environment, and verifies active paths. Completion markers alone never skip
verification. Saved state includes source fingerprints, Git commit, dirty
status, and resulting versions.

For Git source, the rerun command reconciles the configured `sourceRef`.
Change that option and rebuild to select a different ref. All Feature options
are persisted in the image; the rerun command does not replace them.

Upstream editable builds generate schema/template artifacts and may modify
generated files. Ordinary Python edits are live; template/schema edits may
require `make -C python-avd dep`. No Git hooks are installed automatically.
Pre-commit may download isolated Python and Node hook environments on first use.
Molecule and Docker-dependent upstream tests require a consumer-provided usable
Docker daemon, beyond the installed Docker Python client. See
[companion Features](ansible.md#companion-features).
