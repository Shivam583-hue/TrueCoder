#!/bin/sh
set -eu

VERSION="1.0.1"
REPOSITORY="Shivam583-hue/TrueCoder"
WHEEL="truecoder-${VERSION}-py3-none-any.whl"
DEFAULT_RELEASE_BASE="https://github.com/${REPOSITORY}/releases/download/v${VERSION}"
RELEASE_BASE="${TRUECODER_RELEASE_BASE_URL:-$DEFAULT_RELEASE_BASE}"
INSTALL_ROOT="${TRUECODER_INSTALL_DIR:-${XDG_DATA_HOME:-${HOME:?}/.local/share}/truecoder}"
BIN_DIR="${TRUECODER_BIN_DIR:-${HOME:?}/.local/bin}"
VENV_DIR="${INSTALL_ROOT}/venv"

say() {
    printf '%s\n' "$*"
}

fail() {
    printf 'TrueCoder installer: %s\n' "$*" >&2
    exit 1
}

command -v curl >/dev/null 2>&1 || fail "curl is required"

PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
            PYTHON="$candidate"
            break
        fi
    fi
done
[ -n "$PYTHON" ] || fail "Python 3.10 or newer is required"

TEMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/truecoder-install.XXXXXX")
trap 'rm -rf "$TEMP_DIR"' EXIT HUP INT TERM

download() {
    url=$1
    destination=$2
    case "$url" in
        https://*) curl --proto '=https' --tlsv1.2 -fsSL "$url" -o "$destination" ;;
        *) curl -fsSL "$url" -o "$destination" ;;
    esac
}

say "Downloading TrueCoder ${VERSION}..."
download "${RELEASE_BASE}/${WHEEL}" "${TEMP_DIR}/${WHEEL}"
download "${RELEASE_BASE}/SHA256SUMS" "${TEMP_DIR}/SHA256SUMS"

EXPECTED=$(awk -v file="$WHEEL" '$2 == file || $2 == "*" file {print $1}' \
    "${TEMP_DIR}/SHA256SUMS")
[ -n "$EXPECTED" ] || fail "the release checksum for ${WHEEL} is missing"

ACTUAL=$(
    "$PYTHON" -c \
        'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
        "${TEMP_DIR}/${WHEEL}"
)
[ "$EXPECTED" = "$ACTUAL" ] || fail "the downloaded wheel failed checksum verification"

mkdir -p "$INSTALL_ROOT" "$BIN_DIR"
if ! "$PYTHON" -m venv "$VENV_DIR"; then
    fail "Python could not create a virtual environment; install its venv support and retry"
fi

"${VENV_DIR}/bin/python" -m pip install \
    --disable-pip-version-check \
    --upgrade \
    "${TEMP_DIR}/${WHEEL}"
ln -sf "${VENV_DIR}/bin/truecoder" "${BIN_DIR}/truecoder"

INSTALLED=$("${BIN_DIR}/truecoder" --version)
say "Installed ${INSTALLED} at ${BIN_DIR}/truecoder"

case ":${PATH}:" in
    *":${BIN_DIR}:"*) ;;
    *)
        say "Add ${BIN_DIR} to PATH, then run: truecoder"
        say "For this shell: export PATH=\"${BIN_DIR}:\$PATH\""
        ;;
esac
