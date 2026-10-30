# Ansible configuration and integrations

## Collection discovery

Galaxy collections live in the controller environment's Python `site-packages`,
installed with an explicit destination and command-local Galaxy configuration.
Ansible discovers them through Python's search path. Source mode exposes its
real repository collection through `.pth`; a built Galaxy copy does not
replace live source edits.

The Feature does not globally force `ANSIBLE_CONFIG`, `ANSIBLE_COLLECTIONS_PATH`,
or `ANSIBLE_PYTHON_INTERPRETER`. Normal project configuration and local
collection precedence apply. `avd-info` probes the active loader and diagnoses
another AVD collection shadowing the expected copy or consumer-disabled
`collections_scan_sys_path`. Correct the project override or re-enable scanning
when those settings prevent discovery of the Feature's collection.

## Localhost and controller Python

The controller interpreter and remote module interpreter are different choices.
For an explicit local inventory host that should use controller packages:

```yaml
all:
  hosts:
    localhost:
      ansible_connection: local
      ansible_python_interpreter: "{{ ansible_playbook_python }}"
```

Do not apply this setting to arbitrary remote hosts.

## Device and test prerequisites

Runtime dependencies include the integrations used by documented eAPI,
CloudVision, and ANTA workflows. Actual deployment and validation require your
inventory, credentials, devices, and connectivity. Dependency/import checks do
not establish a live deployment result.

`installSshSupport` adds paramiko and libssh Python transports and network CLI
plugin dependencies. It does not install or start an SSH server. Molecule's
Docker plugin and upstream's Docker Python client require a usable daemon for
Docker-dependent tests; a Python package alone does not provide one.

## Companion Features

Compose additional capabilities explicitly in your `features` object. For
example, add the official `ghcr.io/devcontainers/features/docker-in-docker:4`
Feature when a Docker daemon is appropriate for your project.

AVD has no unconditional `dependsOn`; that property cannot be controlled by an
optional flag. Its `installsAfter` entries only order already-selected official
Python/common-utils Features; they do not install them. Select and configure any
other companion Features according to your project's requirements.
