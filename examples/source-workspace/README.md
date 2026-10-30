# Existing AVD source workspace example

Use an existing AVD checkout, with root dependency groups, `python-avd`, and the
real `ansible_collections/arista/avd` tree. From this Feature repository:

```sh
git clone --branch v6.4.0 https://github.com/aristanetworks/avd.git /tmp/avd-workspace
devcontainer up --workspace-folder /tmp/avd-workspace --config "$PWD/examples/source-workspace/.devcontainer/devcontainer.json"
devcontainer exec --workspace-folder /tmp/avd-workspace --config "$PWD/examples/source-workspace/.devcontainer/devcontainer.json" avd-info
```

The explicit config keeps the local Feature reference valid and mounts the AVD
checkout as the lifecycle working directory. `sourcePath` defaults to `.` and is
resolved after mounting. Feature post-create setup completes before consumer
post-create commands, and `waitFor` prevents early assertions. Existing branches,
remotes and edits are preserved. Upstream editable builds can generate artifacts;
read [source lifecycle details](../../docs/source-development.md) before using a working
checkout. The `vscode` user activates the environment without sudo.
