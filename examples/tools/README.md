# Inventory development tools example

From the repository root:

```sh
devcontainer up --workspace-folder examples/tools
devcontainer exec --workspace-folder examples/tools ansible-lint --version
```

This adds the tools requirement set to the released runtime, using one dependency
resolution and the same controller. Change optional switches in
[devcontainer.json](.devcontainer/devcontainer.json) and rebuild to add
`community.general` or SSH transports. Docker-based Molecule tests need a usable
consumer-provided daemon; installing the Python plugin alone does not provide it.
See [composition and prerequisites](../../docs/ansible.md).
