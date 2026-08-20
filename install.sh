#!/bin/sh

set -eu

PACKAGE="super-codex"
DEFAULT_SOURCE="git+ssh://git@github.com/driwand/super-codex.git@main"
SOURCE=${SUPER_CODEX_INSTALL_SOURCE:-$DEFAULT_SOURCE}
MANAGER=""
ACTION="install"

usage() {
    printf '%s\n' \
        "Usage: ./install.sh [install|update|verify|uninstall] [options]" \
        "" \
        "Options:" \
        "  --manager uv|pipx  Select a tool manager explicitly" \
        "  --source SPEC      Install from a Git or local package source" \
        "  -h, --help         Show this help"
}

fail() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

command_exists() {
    command -v "$1" >/dev/null 2>&1
}

uv_owns_package() {
    command_exists uv || return 1
    uv_root=$(uv tool dir 2>/dev/null) || return 1
    [ -d "$uv_root/$PACKAGE" ]
}

pipx_owns_package() {
    command_exists pipx || return 1
    pipx_root=$(pipx environment --value PIPX_HOME 2>/dev/null) || return 1
    [ -d "$pipx_root/venvs/$PACKAGE" ]
}

installed_manager() {
    uv_owner=false
    pipx_owner=false
    if uv_owns_package; then uv_owner=true; fi
    if pipx_owns_package; then pipx_owner=true; fi

    if [ "$uv_owner" = true ] && [ "$pipx_owner" = true ]; then
        fail "$PACKAGE is installed by both uv and pipx; uninstall one copy first"
    fi
    if [ "$uv_owner" = true ]; then
        printf '%s\n' uv
    elif [ "$pipx_owner" = true ]; then
        printf '%s\n' pipx
    fi
}

select_manager() {
    owner=$(installed_manager)
    if [ -n "$MANAGER" ]; then
        if [ -n "$owner" ] && [ "$owner" != "$MANAGER" ]; then
            fail "$PACKAGE is managed by $owner, not $MANAGER"
        fi
        command_exists "$MANAGER" || fail "$MANAGER is not installed or not on PATH"
        printf '%s\n' "$MANAGER"
        return
    fi
    if [ -n "$owner" ]; then
        printf '%s\n' "$owner"
    elif command_exists uv; then
        printf '%s\n' uv
    elif command_exists pipx; then
        printf '%s\n' pipx
    else
        fail "install uv or pipx first, then rerun this command"
    fi
}

verify_installation() {
    command_exists sc || fail "sc is not on PATH"
    command_exists super-codex || fail "super-codex is not on PATH"
    sc --version
    super-codex --version
}

case $(uname -s) in
    Darwin|Linux) ;;
    *) fail "only macOS and Linux are supported" ;;
esac

while [ "$#" -gt 0 ]; do
    case "$1" in
        install|update|verify|uninstall)
            ACTION=$1
            shift
            ;;
        --manager)
            [ "$#" -ge 2 ] || fail "--manager requires uv or pipx"
            MANAGER=$2
            shift 2
            ;;
        --source)
            [ "$#" -ge 2 ] || fail "--source requires a package source"
            SOURCE=$2
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "unknown argument: $1"
            ;;
    esac
done

case "$MANAGER" in
    ""|uv|pipx) ;;
    *) fail "--manager must be uv or pipx" ;;
esac

case "$ACTION" in
    verify)
        verify_installation
        exit 0
        ;;
    install)
        manager=$(select_manager)
        owner=$(installed_manager)
        if [ "$manager" = uv ]; then
            if [ "$owner" = uv ]; then
                uv tool upgrade --reinstall "$PACKAGE"
            else
                uv tool install "$SOURCE"
            fi
        elif [ "$owner" = pipx ]; then
            pipx reinstall "$PACKAGE"
        else
            pipx install "$SOURCE"
        fi
        verify_installation
        ;;
    update)
        [ "$SOURCE" = "$DEFAULT_SOURCE" ] || fail "--source is only valid with install"
        manager=$(installed_manager)
        [ -n "$manager" ] || fail "$PACKAGE is not managed by uv or pipx; run install first"
        if [ -n "$MANAGER" ] && [ "$MANAGER" != "$manager" ]; then
            fail "$PACKAGE is managed by $manager, not $MANAGER"
        fi
        if [ "$manager" = uv ]; then
            uv tool upgrade --reinstall "$PACKAGE"
        else
            pipx reinstall "$PACKAGE"
        fi
        verify_installation
        ;;
    uninstall)
        manager=$(installed_manager)
        [ -n "$manager" ] || fail "$PACKAGE is not managed by uv or pipx"
        if [ -n "$MANAGER" ] && [ "$MANAGER" != "$manager" ]; then
            fail "$PACKAGE is managed by $manager, not $MANAGER"
        fi
        if [ "$manager" = uv ]; then
            uv tool uninstall "$PACKAGE"
        else
            pipx uninstall "$PACKAGE"
        fi
        printf '%s\n' "Kept user configuration and profiles unchanged."
        ;;
esac
