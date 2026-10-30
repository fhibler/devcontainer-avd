#!/usr/bin/env bash
set -euo pipefail
umask 022
phase=preflight
trap 'printf "AVD installation failed during %s (line %s).\n" "$phase" "$LINENO" >&2' ERR

[ "$(id -u)" = 0 ] || {
    echo 'AVD: install as root.' >&2
    exit 1
}
[ "$(uname -s)" = Linux ] || {
    echo 'AVD: Linux is required.' >&2
    exit 1
}
case "$(uname -m)" in
x86_64 | amd64)
    arch=amd64
    target=x86_64
    ;;
aarch64 | arm64)
    arch=arm64
    target=aarch64
    ;;
*)
    echo 'AVD: only amd64 and arm64 are supported.' >&2
    exit 1
    ;;
esac
# /etc/os-release is an OS-owned shell-format file, never Feature input.
read -r os_id os_like < <(
    # Keep os-release's VERSION from overwriting the Feature's VERSION input.
    # shellcheck disable=SC1091
    . /etc/os-release
    printf '%s %s\n' "$ID" "${ID_LIKE:-}"
)
family=
for identity in "$os_id" $os_like; do
    case "$identity" in
    debian | ubuntu)
        family=debian
        break
        ;;
    rhel | centos | fedora | almalinux | rocky)
        family=rhel
        break
        ;;
    esac
done
[ -n "$family" ] || {
    echo 'AVD: Debian/Ubuntu or RHEL-family images are required.' >&2
    exit 1
}
# Validate bootstrap-affecting inputs before any network/package mutation. The
# Python helper validates all options against shipped metadata before setup.
mode=${MODE-runtime}
python_request=${PYTHONVERSION-auto}
case "$mode" in runtime | tools | source) ;; *)
    echo 'AVD: invalid mode.' >&2
    exit 1
    ;;
esac
if [ "$python_request" = auto ]; then
    if [ "$mode" = source ]; then python_request=3.12; else python_request=3.14; fi
fi
case "$python_request" in 3.12 | 3.13 | 3.14 | 3.12.* | 3.13.* | 3.14.*) ;; *)
    echo 'AVD: unsupported Python; use 3.12–3.14 (source: 3.12–3.13).' >&2
    exit 1
    ;;
esac
[[ "$python_request" =~ ^3\.(12|13|14)(\.(0|[1-9][0-9]*))?$ ]] || {
    echo 'AVD: invalid Python version.' >&2
    exit 1
}
if [ "$mode" = source ] && [[ "$python_request" = 3.14* ]]; then
    echo 'AVD: source supports Python 3.12–3.13.' >&2
    exit 1
fi
# Reject contradictory inputs before installing OS prerequisites.
for value in "${INSTALLCOMMUNITYGENERAL-false}" "${INSTALLSSHSUPPORT-false}"; do
    case "$value" in true | false) ;; *)
        echo 'AVD: boolean options require true or false.' >&2
        exit 1
        ;;
    esac
done
version=${VERSION-latest}
if [ "$version" != latest ]; then
    [[ "$version" =~ ^6\.([4-9]|[1-9][0-9]+)\.(0|[1-9][0-9]*)$ ]] || {
        echo 'AVD: select stable AVD >=6.4.0,<7.0.0 or latest.' >&2
        exit 1
    }
fi
for value in "${SOURCEPATH-.}" "${SOURCEREPOSITORY-https://github.com/aristanetworks/avd.git}" "${SOURCEREF-devel}"; do
    if [ -z "$value" ] || [[ "$value" = *$'\n'* || "$value" = *$'\r'* ]]; then
        echo 'AVD: source inputs must be nonempty single-line strings.' >&2
        exit 1
    fi
done
source_repository=${SOURCEREPOSITORY-https://github.com/aristanetworks/avd.git}
if [[ "$source_repository" = *[[:space:]\?\#]* ]]; then
    echo 'AVD: sourceRepository cannot contain whitespace, queries, or fragments.' >&2
    exit 1
fi
case "$source_repository" in
https://*)
    authority=${source_repository#https://}
    authority=${authority%%/*}
    [ -n "$authority" ] && [[ "$authority" != *@* ]] || {
        echo 'AVD: HTTPS sourceRepository cannot contain credentials.' >&2
        exit 1
    }
    ;;
ssh://*)
    authority=${source_repository#ssh://}
    authority=${authority%%/*}
    [ -n "$authority" ] && [[ "$authority" != *:*@* ]] || {
        echo 'AVD: SSH sourceRepository cannot contain passwords.' >&2
        exit 1
    }
    ;;
*)
    [[ "$source_repository" =~ ^[a-zA-Z0-9_.-]+@[a-zA-Z0-9_.-]+:.+$ ]] || {
        echo 'AVD: sourceRepository requires HTTPS or SSH.' >&2
        exit 1
    }
    ;;
esac
source_type=${SOURCETYPE-workspace}
source_ref=${SOURCEREF-devel}
invalid_ref='[[:space:]~^:?*\[\\]'
if [[ "$source_ref" = -* || "$source_ref" =~ $invalid_ref ]]; then
    echo 'AVD: sourceRef must be a branch, tag, or commit without Git revision operators.' >&2
    exit 1
fi
case "$source_type" in workspace | git) ;; *)
    echo 'AVD: invalid sourceType.' >&2
    exit 1
    ;;
esac
if [ "$mode" != source ]; then
    [ "$source_type" = workspace ] && [ "${SOURCEPATH-.}" = . ] && [ "${SOURCEREF-devel}" = devel ] &&
        [ "${SOURCEREPOSITORY-https://github.com/aristanetworks/avd.git}" = https://github.com/aristanetworks/avd.git ] || {
        echo 'AVD: source options are inactive in runtime/tools.' >&2
        exit 1
    }
elif [ "$version" != latest ]; then
    echo 'AVD: version is inactive in source mode.' >&2
    exit 1
elif [ "$source_type" = workspace ]; then
    [ "${SOURCEREF-devel}" = devel ] && [ "${SOURCEREPOSITORY-https://github.com/aristanetworks/avd.git}" = https://github.com/aristanetworks/avd.git ] || {
        echo 'AVD: sourceRef/sourceRepository are inactive for workspace source.' >&2
        exit 1
    }
elif [ "${SOURCEPATH-.}" != . ]; then
    echo 'AVD: sourcePath is inactive for Git source.' >&2
    exit 1
fi
configured_user=${_REMOTE_USER-root}
if ! id -u -- "$configured_user" >/dev/null 2>&1; then
    echo 'AVD: configured remote user does not exist; create it before this Feature.' >&2
    exit 1
fi
if [ -n "${_REMOTE_USER_HOME-}" ]; then
    account_home=$(getent passwd "$configured_user" | cut -d: -f6)
    [ "$_REMOTE_USER_HOME" = "$account_home" ] || {
        echo 'AVD: configured remote home does not match the user account.' >&2
        exit 1
    }
fi
# Existing installs can perform full metadata-driven validation before mutation.
if [ -f /opt/avd/config.json ]; then
    /opt/avd/bootstrap-python /opt/avd/feature/avd_setup.py preflight
fi
printf 'AVD preflight: mode=%s Linux/%s OS=%s Python=%s (resolved versions pending)\n' "$mode" "$arch" "$os_id" "$python_request"
printf '%s\n' 'This Dev Container Feature is not officially supported by Arista Networks.'
printf '%s\n' 'Please open issues at https://github.com/fhibler/devcontainer-avd/issues.'

# Invalidate old success before any in-place installation can fail.
if [ -f /opt/avd/config.json ]; then
    /opt/avd/bootstrap-python /opt/avd/feature/avd_setup.py invalidate
fi
phase=os-packages
source_packages=()
# Real contributor docs need zoneinfo data; nodeenv's Node binary needs libatomic.
if [ "$mode" = source ]; then
    if [ "$family" = debian ]; then source_packages=(tzdata libatomic1); else source_packages=(tzdata libatomic); fi
fi
if [ "$family" = rhel ]; then
    rhel_curl=curl-minimal
    rhel_coreutils=coreutils
    if rpm -q --quiet curl; then rhel_curl=curl; fi
    if rpm -q --quiet coreutils-single; then rhel_coreutils=coreutils-single; fi
fi
if [ "$family" = debian ]; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends ca-certificates curl git make tar gzip unzip coreutils findutils openssh-client "${source_packages[@]}"
    rm -rf /var/lib/apt/lists/*
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y ca-certificates "$rhel_curl" git-core make tar gzip unzip "$rhel_coreutils" findutils openssh-clients "${source_packages[@]}"
    dnf clean all
elif command -v microdnf >/dev/null 2>&1; then
    microdnf install -y ca-certificates "$rhel_curl" git-core make tar gzip unzip "$rhel_coreutils" findutils openssh-clients "${source_packages[@]}"
    microdnf clean all
else
    echo 'AVD: supported RHEL image must supply dnf or microdnf.' >&2
    exit 1
fi

phase=bootstrap
feature_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=src/avd/bootstrap.env
. "$feature_dir/bootstrap.env"
install -d -m 0755 /opt/avd/feature /opt/avd/bootstrap /opt/avd/python
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
curl --fail --silent --show-error --location --connect-timeout 20 --max-time 180 --retry 2 \
    "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${target}-unknown-linux-gnu.tar.gz" -o "$work/uv.tar.gz"
if [ "$arch" = amd64 ]; then checksum=$UV_SHA256_AMD64; else checksum=$UV_SHA256_ARM64; fi
if ! printf '%s  %s\n' "$checksum" "$work/uv.tar.gz" | sha256sum --check --status; then
    echo 'AVD: uv archive checksum mismatch; refusing execution.' >&2
    exit 1
fi
# Only execute the archive after checksum verification.
tar -xzf "$work/uv.tar.gz" --strip-components=1 -C "$work" "uv-${target}-unknown-linux-gnu/uv"
install -m 0755 "$work/uv" /opt/avd/bootstrap/uv
export UV_PYTHON_INSTALL_DIR=/opt/avd/python UV_PYTHON_PREFERENCE=only-managed UV_PYTHON_DOWNLOADS=automatic UV_CACHE_DIR=/opt/avd/cache/uv
/opt/avd/bootstrap/uv python install "$python_request" --no-bin --no-progress
python_path=$(/opt/avd/bootstrap/uv python find "$python_request")
ln -sfn "$python_path" /opt/avd/bootstrap-python
cp "$feature_dir/avd_setup.py" "$feature_dir/devcontainer-feature.json" "$feature_dir/bootstrap.env" "$feature_dir/tooling-requirements.txt" /opt/avd/feature/
install -m 0755 "$feature_dir/avd-command" /usr/local/bin/avd-info
install -m 0755 "$feature_dir/avd-command" /usr/local/bin/avd-setup-source
phase=environment
/opt/avd/bootstrap-python /opt/avd/feature/avd_setup.py install
