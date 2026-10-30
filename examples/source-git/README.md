# Git source example

From the repository root:

```sh
devcontainer up --workspace-folder examples/source-git
devcontainer exec --workspace-folder examples/source-git avd-info
```

This clones the exact `v6.4.0` tag into the ordinary user's Feature-owned source
directory. For a moving development checkout, set `sourceRef` to `devel` and
rebuild. For immutable repeatability use a full commit hash instead of a tag.
The resolved commit is recorded in the manifest. Private repositories use normal
Git/SSH credential facilities; credential-bearing Feature options are rejected.
Run `avd-setup-source` explicitly after dependency edits. A dirty checkout is
preserved, and a conflicting ref change fails with recovery instructions.
See [source development](../../docs/source-development.md).
