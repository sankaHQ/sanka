#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Sanka installer for macOS and Linux. No system Python, sudo, or profile edits.
set -eu

main() {
    version=0.2.13
    coexist=0
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --version) [ "$#" -ge 2 ] || fail '--version requires a version'; version=$2; shift 2 ;;
            --coexist) coexist=1; shift ;;
            --help|-h)
                printf '%s\n' 'Usage: sh install.sh [--version X.Y.Z] [--coexist]' \
                    'SANKA_INSTALL_DIR: runtime directory (default ~/.local/share/sanka/cli)' \
                    'SANKA_BIN_DIR: executable directory (default ~/.local/bin)' \
                    'Existing package-manager installations are preserved. --coexist permits a second installation.'
                return ;;
            *) fail "Unknown argument: $1" ;;
        esac
    done
    case "$version" in ''|*[!0-9.]*|.*|*..*|*.) fail 'Use an exact stable version such as 0.2.13' ;; esac
    case "$(uname -s)" in Darwin|Linux) ;; *) fail 'Use uv tool install --python 3.12 sanka-cli on this platform.' ;; esac
    root=${SANKA_INSTALL_DIR:-"$HOME/.local/share/sanka/cli"}
    bin=${SANKA_BIN_DIR:-"$HOME/.local/bin"}
    case "$root:$bin" in *"'"*|*'
'*) fail 'Installation paths must not contain quotes or newlines' ;; esac
    case "$root" in /*) ;; *) fail 'SANKA_INSTALL_DIR must be absolute' ;; esac
    case "$bin" in /*) ;; *) fail 'SANKA_BIN_DIR must be absolute' ;; esac
    target="$bin/sanka"
    managed="$root/current/bin/sanka"
    if [ -e "$target" ] || [ -L "$target" ]; then
        [ -L "$target" ] && [ "$(readlink "$target")" = "$managed" ] ||
            fail "$target belongs to another installation. Keep using its package manager, or choose a different SANKA_BIN_DIR with --coexist. Nothing was replaced."
    fi
    existing=$(command -v sanka || true)
    if [ -n "$existing" ] && [ "$existing" != "$target" ] && [ "$coexist" -eq 0 ]; then
        fail "Existing Sanka: $existing. Homebrew users: brew upgrade sankaHQ/cli/sanka. uv users: uv tool upgrade sanka-cli --python 3.12. To keep a second installation, rerun with --coexist."
    fi
    [ ! -L "$root" ] || fail 'SANKA_INSTALL_DIR must not be a symlink'
    umask 077
    mkdir -p "$root" "$bin"
    mkdir "$root/install.lock" 2>/dev/null || fail "Another installation is active, or an interrupted install left $root/install.lock. Check before removing that empty lock directory."
    trap 'rm -f "$root/install.lock/uv-install.sh" "$root/install.lock/current"; rmdir "$root/install.lock"' EXIT
    trap 'exit 1' HUP INT TERM
    uv="$root/bootstrap/uv"
    if [ ! -x "$uv" ]; then
        command -v curl >/dev/null 2>&1 || fail 'curl is required to download the installer.'
        bootstrap="$root/install.lock/uv-install.sh"
        curl --proto '=https' --tlsv1.2 -fLsS --retry 2 --connect-timeout 10 --max-time 120 \
            https://astral.sh/uv/0.11.21/install.sh -o "$bootstrap"
        if command -v sha256sum >/dev/null 2>&1; then
            digest=$(sha256sum "$bootstrap")
        elif command -v shasum >/dev/null 2>&1; then
            digest=$(shasum -a 256 "$bootstrap")
        else
            rm "$bootstrap"
            fail 'sha256sum or shasum is required to verify the download.'
        fi
        case "$digest" in 053045e1e69ec77358fd44f2ef2cacb768a22d50f433e213624f0157ffbbc883\ *) ;;
            *) rm "$bootstrap"; fail 'uv installer checksum mismatch; nothing was executed.' ;; esac
        UV_UNMANAGED_INSTALL="$root/bootstrap" sh "$bootstrap"
        rm "$bootstrap"
    fi
    release="$root/versions/$version"
    # Ignore a project's Python and index settings; keep the CLI runtime private.
    unset VIRTUAL_ENV PYTHONPATH PYTHONHOME UV_PYTHON UV_INDEX UV_EXTRA_INDEX_URL UV_INDEX_URL UV_FIND_LINKS UV_NO_INDEX UV_CONFIG_FILE
    export UV_PYTHON_INSTALL_DIR="$root/python" UV_TOOL_DIR="$release/tools"
    export UV_TOOL_BIN_DIR="$release/bin" UV_CACHE_DIR="$root/cache"
    "$uv" tool install --no-config --managed-python --python 3.12 \
        --default-index https://pypi.org/simple "sanka-cli==$version"
    executable="$release/bin/sanka"
    actual=$("$executable" --version)
    [ "$actual" = "sanka, version $version" ] || fail "Version check failed: $actual"
    "$executable" --help >/dev/null
    "$release/tools/sanka-cli/bin/python" -c \
        'import sys; from importlib.metadata import version; assert sys.version_info[:2] == (3, 12); assert version("sanka-cli") == sys.argv[1]' "$version"
    # Switch only after the new environment passes. Previous environments remain usable.
    [ ! -e "$root/current" ] || [ -L "$root/current" ] || fail "$root/current is not an installer-owned symlink"
    if [ -e "$target" ] || [ -L "$target" ]; then
        [ -L "$target" ] && [ "$(readlink "$target")" = "$managed" ] ||
            fail "Another installation changed $target during setup. Nothing was replaced."
    else
        ln -s "$managed" "$target"
    fi
    ln -s "$release" "$root/install.lock/current"
    "$release/tools/sanka-cli/bin/python" -c \
        'import os,sys; os.replace(sys.argv[1],sys.argv[2])' "$root/install.lock/current" "$root/current"
    [ "$("$target" --version)" = "sanka, version $version" ] || fail "The installed command no longer selects $version"
    printf '\n%s\n' "Installed Sanka $version: $target" "Runtime: $root/python"
    selected=$(command -v sanka || true)
    if [ "$selected" = "$target" ]; then
        "$target" --version
    else
        printf '%s\n' "PATH currently selects: ${selected:-no sanka command}" \
            "Run directly: '$target' --help" \
            "To use this installation in this shell: export PATH='$bin':\"\$PATH\""
    fi
    printf '%s\n' 'In an already-open zsh terminal run rehash; in bash run hash -r.'
}

fail() { printf 'Sanka installation: %s\n' "$*" >&2; exit 1; }
main "$@"
