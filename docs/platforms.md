# Platform verification matrix

The installer recognizes Debian/Ubuntu and RHEL-family identities, including
multi-value `ID_LIKE`, using apt-get, dnf, or microdnf. Required images on **both
native amd64 and arm64** are generated from [test/images.json](../test/images.json):

<!-- images:start -->
- `debian:12`
- `debian:12-slim`
- `debian:13`
- `debian:13-slim`
- `debian:latest`
- `debian:stable-slim`
- `ubuntu:22.04`
- `ubuntu:24.04`
- `ubuntu:26.04`
- `ubuntu:26.10`
- `ubuntu:latest`
- `ubuntu:rolling`
- `almalinux:9`
- `almalinux:9-minimal`
- `almalinux:10`
- `almalinux:10-minimal`
- `almalinux:9.8`
- `almalinux:10.2`
- `almalinux:latest`
- `quay.io/rockylinux/rockylinux:9`
- `quay.io/rockylinux/rockylinux:9-minimal`
- `quay.io/rockylinux/rockylinux:10`
- `quay.io/rockylinux/rockylinux:10-minimal`
- `quay.io/rockylinux/rockylinux:10.2`
- `registry.access.redhat.com/ubi9/ubi:latest`
- `registry.access.redhat.com/ubi9/ubi-minimal:latest`
- `registry.access.redhat.com/ubi10/ubi:latest`
- `registry.access.redhat.com/ubi10/ubi-minimal:latest`
- `quay.io/centos/centos:stream9`
- `quay.io/centos/centos:stream10`
<!-- images:end -->

Moving tags are pulled afresh; CI records their resolved digest and architecture.
Minimal images must supply their family package manager. Unknown architectures,
unsupported distribution families, unavailable Python patches, missing native
wheels, incompatible dependencies, and contradictory options fail explicitly.
No speculative compiler toolchains or unrelated repositories are installed.

Release support requires every required job to pass on both native
architectures. A local run on one architecture does not establish support for
the other. Configure native arm64 runner availability for the repository;
emulation does not replace native integration evidence. See
[CI and local testing](development.md).
