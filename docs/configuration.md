# Configuration and environment

The [README option table](../README.md#options) is generated from
[Feature metadata](../src/avd/devcontainer-feature.json), the authority for option
names, types, defaults, and descriptions. Configure these options in
`devcontainer.json`; changing them requires rebuilding the container.

## Option combinations

All defaults exist in every mode. Reports label inactive fields explicitly.
Non-default inactive fields fail rather than being ignored:

- `runtime` and `tools`: leave `sourceType`, `sourcePath`, `sourceRepository`, and `sourceRef` at their defaults.
- `source`: leave `version` at `latest`; the checkout supplies AVD.
- `source` with `sourceType: workspace`: leave `sourceRepository` and `sourceRef` at their defaults. Existing branches and remotes are preserved.
- `source` with `sourceType: git`: leave `sourcePath` at `.`. Use `sourceRepository` and `sourceRef` to choose the checkout.

Feature inputs use the standard uppercase environment mapping, for example
`mode` → `MODE`, `pythonVersion` → `PYTHONVERSION`, and
`installSshSupport` → `INSTALLSSHSUPPORT`. These are installer inputs, not runtime
switches for an existing container. There is no second set of aliases.

## AVD and dependency versions

`version: latest` selects the highest stable, non-yanked AVD release available on
**both** PyPI and Galaxy. PyAVD and `arista.avd` always use the same exact released
version. Support starts at AVD 6.4.0 and covers compatible 6.x releases. A newer
unsupported major fails with an update instruction, rather than silently
selecting an older major. Exact requests must exist on both indexes; ranges and
prereleases are rejected.

Runtime installs `pyavd[ansible]`, `anta[cli]`, and Galaxy collection dependencies.
PyAVD supplies runtime Python requirements, including CloudVision and ANTA
integrations. Python and collection requirements are verified independently.
Tools adds the single maintained
[tooling requirement set](../src/avd/tooling-requirements.txt), resolved together
with released PyAVD and its Ansible bounds. It keeps released PyAVD in place.

`installCommunityGeneral` adds a `community.general` release compatible with the
resolved controller Ansible. `installSshSupport` adds `paramiko` and
`ansible-pylibssh` client transports. See [external prerequisites](ansible.md).

An exact AVD version does not freeze every transitive Python or Galaxy
dependency. Retain the resolved environment manifest for diagnosis. A Feature
version or immutable OCI digest pins the Feature payload, independently of AVD
and its downloaded dependencies.

## Managed Python and paths

| Mode | `pythonVersion: auto` | Explicit supported selections |
| --- | --- | --- |
| `runtime`, `tools` | 3.14 | 3.12–3.14, as a minor or exact patch |
| `source` | 3.12 | 3.12–3.13, as a minor or exact patch |

A minor resolves to the exact patch available to the pinned bootstrap. Explicit
unavailable patches fail. Source rejects Python 3.14 because the contributor
toolchain includes older isolated Ansible documentation environments. Broader
upstream Python support does not establish contributor-hook compatibility.

Managed CPython lives under `/opt/avd/python`, installed by a private,
checksum-verified `uv`. It does not replace system Python or another Feature's
`uv`. The stable public interpreter is `/opt/avd/venv/bin/python`; the Feature
prepends its `bin` directory to `PATH` and selects it for editor Python settings.

Runtime/tools environments are root-owned and usable by ordinary users. Source's
`/opt/avd/venv` symlink points to the configured user's
`~/.local/share/avd/venv`, created during post-create setup without sudo. That
user must already exist; root is valid. Setup respects the configured remote
user/home and later UID adjustment without changing workspace ownership.

Installations update packages in place at their final paths. A failed setup
removes previous success state; it does not promise package rollback. Repeated
identical installation reruns verification. Contradictory duplicate Feature
options fail with an instruction to use one option set and rebuild.

## Inspection and manifests

Run `avd-info` for a readable report, or `avd-info --json` for structured output.
The command probes the environment using its own interpreter and active Ansible
loader, and distinguishes current source state from the last successful setup.
Before source activation, it reports deferred status.

Reports contain OS/release, raw and normalized architecture, libc, package
manager, host-provided kernel, phase, Feature/options, user/home/UID/GID, exact
interpreter, package/collection versions and paths, Ansible configuration, and
source revision/dirty status. Build-time source values are deferred and
inapplicable options are inactive. Displayed reports and diagnostics redact
secrets. Operational configuration and successful state preserve exact paths,
versions, and SSH usernames; repository options reject embedded credentials.
Setup never dumps the process environment or persists source/index authentication.

Successful manifests live at `/opt/avd/state.json` for runtime/tools and
`~/.local/share/avd/state.json` for source. Failed setup removes its completion
state. `avd-info` identifies disabled Python-path scanning, shadowed AVD copies,
missing interpreters, required tools/SSH packages or optional collections, and
source identity changes. Installation and inspection share the same environment
checks; inspection does not simply replay a stale manifest.
