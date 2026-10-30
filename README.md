# Arista AVD Dev Container Feature

Add Arista AVD to your Dev Container with managed Python, Ansible, ANTA's CLI,
and matching PyAVD and `arista.avd` releases. Use it to generate configurations,
develop inventories and playbooks, or contribute to AVD itself.

**This Dev Container Feature is not officially supported by Arista Networks.**
Please open issues in this [GitHub repository](https://github.com/fhibler/devcontainer-avd/issues).

## Add the Feature

Add `ghcr.io/fhibler/devcontainer-avd/avd:1` to the `features` object in your
project's `.devcontainer/devcontainer.json`. For a new Dev Container:

```json
{
  "name": "AVD",
  "image": "mcr.microsoft.com/devcontainers/base:ubuntu-24.04",
  "remoteUser": "vscode",
  "features": {
    "ghcr.io/fhibler/devcontainer-avd/avd:1": {}
  }
}
```

Open the project with Dev Containers, or rebuild your existing container after
adding the Feature. A container engine and a Dev Containers editor or CLI are
required. The Feature works with Debian, Ubuntu, and RHEL-family base images on
Linux amd64 and arm64; see the [platform matrix](docs/platforms.md).

With no options, you get the latest compatible released AVD runtime. Inside the
container, run `avd-info` to inspect installed versions, or use `ansible-playbook`
and `anta` directly. Python packages use `/opt/avd/venv/bin/python`, which is also
selected for the editor.

The `:1` tag selects the Feature's major version; `:1.0.0` pins Feature 1.0.0.
The `version` option below separately selects the AVD release.

## Choose a mode

| `mode` | Use it for | Included |
| --- | --- | --- |
| `runtime` (default) | Running AVD playbooks and generating configurations | Released PyAVD, matching AVD collection and dependencies, Ansible, ANTA CLI, Git and Make. |
| `tools` | Developing inventories and playbooks with released AVD | Runtime plus ansible-lint, yamllint, Ruff, pre-commit, pytest, Tox, Molecule and its Docker plugin. |
| `source` | Contributing to AVD | Editable PyAVD, the live collection, and development/documentation/coverage dependencies from an AVD checkout. |

Set options inside the Feature object. For example, to use development tools
with an exact AVD release and optional collections and SSH transports:

```json
{
  "features": {
    "ghcr.io/fhibler/devcontainer-avd/avd:1": {
      "mode": "tools",
      "version": "6.4.0",
      "pythonVersion": "3.13",
      "installCommunityGeneral": true,
      "installSshSupport": true
    }
  }
}
```

For `source`, choose an existing workspace checkout or a Git repository and ref;
see [source setup](docs/source-development.md) for complete configurations.
Runtime and tools install during the image build. Source activates after the
workspace is mounted, before your `postCreateCommand`. Rebuild the container
after changing Feature options.

## Options

The table is generated from the Feature metadata. Omit options to use their
defaults; leave options for other modes at their defaults.

<!-- options:start -->
| Option | Type | Default | Description |
| --- | --- | --- | --- |
| `mode` | string | `runtime` | Released runtime, inventory development tools, or editable AVD source. Allowed values: `runtime`, `tools`, `source`. |
| `version` | string | `latest` | Exact stable AVD >=6.4.0,<7.0.0, or latest stable release common to PyPI and Galaxy (runtime/tools). |
| `pythonVersion` | string | `auto` | Managed CPython minor or exact patch. Auto: 3.14 runtime/tools, 3.12 source. Runtime/tools: 3.12–3.14; source: 3.12–3.13. |
| `sourceType` | string | `workspace` | Source checkout location (source only). Allowed values: `workspace`, `git`. |
| `sourcePath` | string | `.` | Workspace repository root relative to post-create working directory, or absolute path (source/workspace). |
| `sourceRepository` | string | `https://github.com/aristanetworks/avd.git` | HTTPS or SSH Git location without embedded credentials (source/git). |
| `sourceRef` | string | `devel` | Branch, tag, or commit to resolve (source/git). |
| `installCommunityGeneral` | boolean | `false` | Add a community.general version compatible with the controller's Ansible. |
| `installSshSupport` | boolean | `false` | Add paramiko and ansible-pylibssh client transports; does not provide an SSH server. |
<!-- options:end -->

See [configuration details](docs/configuration.md) for version resolution,
Python compatibility, option combinations, environment paths, and reports.
Real deployment and validation require your inventory, credentials, devices,
and connectivity. Docker-based tests require a usable Docker daemon; see
[Ansible configuration and companion Features](docs/ansible.md).

## Examples and documentation

- Runnable examples: [runtime](examples/runtime/README.md), [tools](examples/tools/README.md), [workspace source](examples/source-workspace/README.md), and [Git source](examples/source-git/README.md).
- [Configuration](docs/configuration.md): versions, option rules, Python environment, and inspection.
- [Source development](docs/source-development.md): workspace/Git setup, live edits, lifecycle, and dependency updates.
- [Ansible and integrations](docs/ansible.md): collection discovery, localhost Python, device requirements, and Feature composition.
- [Platforms](docs/platforms.md): image and architecture matrix.
- [Development and testing](docs/development.md): local checks, CI jobs, and maintenance.
- [Releases](docs/releases.md): manual publishing, artifact verification, and recovery.

Licensed under [MIT](LICENSE). Upstream packages and source retain their own licenses.
