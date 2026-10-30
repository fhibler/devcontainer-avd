# Released runtime example

From the repository root, run:

```sh
devcontainer up --workspace-folder examples/runtime
devcontainer exec --workspace-folder examples/runtime avd-info
```

Or open this folder in a Dev Containers editor. This selects released AVD on a
standard development image as the ordinary `vscode` user. The local Feature path
is resolved from the checked-in configuration; no Dockerfile or requirements
file is needed. A container engine and the Dev Container CLI/editor are required.
See [configuration](../../docs/configuration.md) for exact versions, local
interpreter settings and project configuration precedence.
