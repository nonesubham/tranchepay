#!/usr/bin/env bash
#
# pre_publish_check.sh - local PyPI readiness gate for tranchepay.
#
# Run this BEFORE `twine upload` on every release. It proves, on your machine,
# that the artefacts you are about to publish are installable, correctly
# described, and free of the usual packaging mistakes.
#
#   ./scripts/pre_publish_check.sh                  # verify everything
#   ./scripts/pre_publish_check.sh --keep-venv      # keep the throwaway venvs
#   ./scripts/pre_publish_check.sh --upload-testpypi # ...then upload to TestPyPI
#
# Written for Bash 3.2+ so it works with the stock /bin/bash on macOS, and for
# Bash 5.x on Linux. Requires: python3 (>=3.10), a network connection for the
# build's isolated environment, and nothing else.
#
# Exit codes: 0 = ready to publish, 1 = a check failed, 2 = bad usage.
#
set -Eeuo pipefail

# --------------------------------------------------------------------------
# Layout. Everything is resolved relative to the repository root so the script
# behaves the same no matter where it is invoked from.
# --------------------------------------------------------------------------
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

PACKAGE="tranchepay"
SRC_DIR="src/${PACKAGE}"
BUILD_VENV=".venv-build"   # holds build/twine/validate-pyproject tooling
SMOKE_VENV=".venv-test"    # pristine env used to install the built wheel

UPLOAD_TESTPYPI=0
KEEP_VENV=0

usage() {
  cat <<'USAGE'
Usage: scripts/pre_publish_check.sh [options]

Options:
  --upload-testpypi   after all checks pass, run `twine upload --repository testpypi dist/*`
  --keep-venv         do not delete .venv-build / .venv-test when finished
  -h, --help          show this help
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --upload-testpypi) UPLOAD_TESTPYPI=1 ;;
    --keep-venv)       KEEP_VENV=1 ;;
    -h|--help)         usage; exit 0 ;;
    *) printf 'error: unknown option: %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

# --------------------------------------------------------------------------
# Output helpers. Colour only when stdout is a terminal, so CI logs stay clean.
# --------------------------------------------------------------------------
if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_RED=$'\033[31m'
  C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_BLUE=$'\033[34m'
else
  C_RESET=''; C_BOLD=''; C_RED=''; C_GREEN=''; C_YELLOW=''; C_BLUE=''
fi

STEP=0
step() { STEP=$(( STEP + 1 )); printf '\n%s==> [%d/%d] %s%s\n' "$C_BOLD$C_BLUE" "$STEP" "$TOTAL_STEPS" "$*" "$C_RESET"; }
info() { printf '     %s\n' "$*"; }
ok()   { printf '     %sPASS%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '     %sWARN%s %s\n' "$C_YELLOW" "$C_RESET" "$*"; }
die()  { printf '\n%sFAIL%s %s\n' "$C_RED$C_BOLD" "$C_RESET" "$*" >&2; exit 1; }

TOTAL_STEPS=10

# Remove a path, but only inside the project. A typo in a variable must never
# turn into `rm -rf /` or a deleted home directory.
remove_path() {
  local target="$1"
  case "$target" in
    "$PROJECT_ROOT"/*) rm -rf -- "$target" ;;
    *) die "refusing to remove '$target': outside $PROJECT_ROOT" ;;
  esac
}

cleanup() {
  local status=$?
  if [[ "$KEEP_VENV" -eq 0 ]]; then
    if [[ -d "$PROJECT_ROOT/$BUILD_VENV" ]]; then remove_path "$PROJECT_ROOT/$BUILD_VENV"; fi
    if [[ -d "$PROJECT_ROOT/$SMOKE_VENV" ]]; then remove_path "$PROJECT_ROOT/$SMOKE_VENV"; fi
  fi
  exit "$status"
}
trap cleanup EXIT

printf '%s%s pre-publish check: %s %s\n' "$C_BOLD" "$C_BLUE" "$PACKAGE" "$C_RESET"
info "repository: $PROJECT_ROOT"

# ==========================================================================
step "Preconditions: interpreter, repository state"
# ==========================================================================
command -v python3 >/dev/null 2>&1 || die "python3 not found on PATH"
PY_VERSION="$(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
info "python3: $PY_VERSION"

python3 - <<'PY' || die "Python >= 3.10 is required (requires-python in pyproject.toml)"
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
ok "interpreter is new enough"

# A dirty tree means the sdist will not match any commit. Not fatal (you may be
# testing a work-in-progress), but publishing from a dirty tree is how "which
# code is on PyPI?" incidents start.
if [[ -d .git ]]; then
  if [[ -n "$(git status --porcelain)" ]]; then
    warn "git working tree is dirty; the sdist will not match a commit"
  else
    ok "git working tree is clean"
  fi
fi

# ==========================================================================
step "Tooling: build, twine, validate-pyproject in an isolated venv"
# ==========================================================================
# Pinned to its own venv so the release tools never leak into your dev env and
# a stale twine cannot produce a false pass.
[[ -d "$BUILD_VENV" ]] && remove_path "$PROJECT_ROOT/$BUILD_VENV"
python3 -m venv "$BUILD_VENV" || die "could not create $BUILD_VENV"
"$BUILD_VENV/bin/python" -m pip install --quiet --upgrade pip
"$BUILD_VENV/bin/python" -m pip install --quiet --upgrade \
  "build" "twine" "validate-pyproject[all]"
info "build            $( "$BUILD_VENV/bin/python" -c 'import build; print(build.__version__)' )"
info "twine            $( "$BUILD_VENV/bin/python" -m twine --version | awk '{print $2}' )"
info "twine is the tool that talks to PyPI; keep it current - PyPI rejects old uploads."
ok "release tooling installed in $BUILD_VENV"

# ==========================================================================
step "Repository structure: required files are present"
# ==========================================================================
missing=0
for f in "pyproject.toml" "README.md" "LICENSE" "$SRC_DIR/__init__.py" "$SRC_DIR/py.typed"; do
  if [[ -f "$f" ]]; then
    ok "$f"
  else
    warn "missing: $f"
    missing=1
  fi
done
[[ "$missing" -eq 0 ]] || die "essential files are missing; see warnings above"

if [[ -f setup.py || -f setup.cfg ]]; then
  warn "setup.py/setup.cfg present: metadata may be split across files"
else
  ok "single source of metadata (pyproject.toml, no setup.py/cfg)"
fi

# ==========================================================================
step "Version: __version__ is present and PEP 440 compliant"
# ==========================================================================
VERSION="$(python3 - "$SRC_DIR/__init__.py" <<'PY'
import ast, sys, pathlib
tree = ast.parse(pathlib.Path(sys.argv[1]).read_text())
for node in tree.body:
    if isinstance(node, ast.Assign) and any(
        getattr(t, "id", None) == "__version__" for t in node.targets
    ):
        print(ast.literal_eval(node.value))
        break
PY
)"
[[ -n "$VERSION" ]] || die "could not find __version__ in $SRC_DIR/__init__.py"
"$BUILD_VENV/bin/python" - "$VERSION" <<'PY' || die "version is not valid PEP 440"
import sys
from packaging.version import InvalidVersion, Version
try:
    Version(sys.argv[1])
except InvalidVersion:
    raise SystemExit(1)
PY
info "version: $VERSION"
if [[ "$VERSION" =~ (dev|a[0-9]|b[0-9]|rc[0-9]|post) ]]; then
  warn "pre-release version: PyPI will hide it from 'pip install tranchepay' by default"
fi
if command -v git >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; then
  if git rev-parse -q --verify "refs/tags/v$VERSION" >/dev/null 2>&1; then
    ok "tag v$VERSION exists"
  else
    warn "no git tag v$VERSION yet; tag the commit you are publishing"
  fi
fi
ok "version string is publishable"

# ==========================================================================
step "Clean: remove stale build/, dist/, egg-info"
# ==========================================================================
remove_path "$PROJECT_ROOT/dist"
remove_path "$PROJECT_ROOT/build"
for egg in "$PROJECT_ROOT"/src/*.egg-info "$PROJECT_ROOT"/*.egg-info; do
  [[ -d "$egg" ]] && remove_path "$egg"
done
ok "previous artefacts removed (stale wheels are the #1 cause of 'old' uploads)"

# ==========================================================================
step "Build: sdist + wheel via python -m build"
# ==========================================================================
# `python -m build` builds the sdist first and then builds the wheel FROM that
# sdist, in an isolated environment with the build-system requires pinned in
# pyproject.toml. If the sdist were missing a file the wheel build would fail
# here, so this single command validates the sdist too.
"$BUILD_VENV/bin/python" -m build || die "build failed - see the output above"
ok "build finished"

# Globs (with nullglob) rather than `find | head`, which would trip `pipefail`
# if a stray artefact ever made the pipeline close early.
shopt -s nullglob
sdists=(dist/*.tar.gz)
wheels=(dist/*.whl)
shopt -u nullglob

[[ "${#sdists[@]}" -eq 1 ]] || die "expected exactly one sdist, found ${#sdists[@]}"
[[ "${#wheels[@]}" -eq 1 ]] || die "expected exactly one wheel, found ${#wheels[@]}"
SDIST="${sdists[0]}"
WHEEL="${wheels[0]}"
info "sdist: $SDIST"
info "wheel: $WHEEL"
ok "exactly one sdist and one wheel"

# ==========================================================================
step "Artefact integrity: metadata and contents inside the archives"
# ==========================================================================
# Read the archives directly rather than trusting the build log: this catches a
# metadata field that never made it into the wheel, or a file the wheel forgot
# to ship (py.typed is the classic one, and its absence silently breaks typing
# for every downstream user).
"$BUILD_VENV/bin/python" - "$WHEEL" "$SDIST" "$PACKAGE" <<'PY' || die "artefact contents are wrong"
import email.parser, sys, tarfile, zipfile

wheel_path, sdist_path, package = sys.argv[1:4]
problems, notes = [], []

with zipfile.ZipFile(wheel_path) as whl:
    names = whl.namelist()
    meta_name = next((n for n in names if n.endswith(".dist-info/METADATA")), None)
    if meta_name is None:
        problems.append("wheel has no .dist-info/METADATA")
        meta = {}
    else:
        meta = email.parser.BytesParser().parsebytes(whl.read(meta_name))

    for required in (f"{package}/__init__.py", f"{package}/py.typed"):
        if required not in names:
            problems.append(f"wheel is missing {required}")
    if any(n.startswith("tests/") for n in names):
        notes.append("wheel ships tests/ (not harmful, just extra weight)")

expected = {"Name": package, "Requires-Python": None, "Requires-Dist": None}
for field in ("Name", "Version", "Requires-Python", "Requires-Dist",
              "Description-Content-Type", "License-Expression", "License", "Summary"):
    values = meta.get_all(field) or []
    for value in values:
        print(f"     {field}: {value}")

if meta.get("Name") != package:
    problems.append(f"Name is {meta.get('Name')!r}, expected {package!r}")
if not meta.get("Version"):
    problems.append("no Version in metadata")
if meta.get("Description-Content-Type") != "text/markdown":
    problems.append("README is not declared as text/markdown (long_description breaks on PyPI)")
requires = " ".join(meta.get_all("Requires-Dist") or [])
for dep in ("razorpay", "pydantic"):
    if dep not in requires:
        problems.append(f"runtime dependency {dep!r} is not declared")

with tarfile.open(sdist_path) as tar:
    sdist_names = tar.getnames()
for required in ("pyproject.toml", "PKG-INFO", "README.md", "LICENSE",
                 f"src/{package}/__init__.py", f"src/{package}/py.typed"):
    if not any(n.endswith(required) for n in sdist_names):
        problems.append(f"sdist is missing {required}")

for note in notes:
    print(f"     WARN {note}")
for problem in problems:
    print(f"     FAIL {problem}")
raise SystemExit(1 if problems else 0)
PY
ok "metadata complete; py.typed, LICENSE and README all shipped"

# ==========================================================================
step "Metadata validation: twine check + validate-pyproject"
# ==========================================================================
# `twine check` renders the README the way PyPI will and fails on malformed
# metadata. Without --strict it only reports errors; with it, warnings fail too,
# which is what you want on a release.
"$BUILD_VENV/bin/python" -m twine check --strict dist/* \
  || die "twine check failed - fix the README or metadata (see above)"
ok "twine check --strict passed"

# validate-pyproject checks pyproject.toml against the PEP 621/639 schemas and
# the setuptools plugin, i.e. exactly what the build backend must accept.
"$BUILD_VENV/bin/python" -m validate_pyproject pyproject.toml \
  || die "validate-pyproject rejected pyproject.toml"
ok "validate-pyproject passed"

# ==========================================================================
step "Smoke test: install the wheel into a pristine venv"
# ==========================================================================
# The build venv already has the tooling and possibly an editable install, so a
# separate throwaway venv is the only way to prove the WHEEL is self-sufficient:
# its declared dependencies must be enough to import the package.
[[ -d "$SMOKE_VENV" ]] && remove_path "$PROJECT_ROOT/$SMOKE_VENV"
python3 -m venv "$SMOKE_VENV" || die "could not create $SMOKE_VENV"
"$SMOKE_VENV/bin/python" -m pip install --quiet --upgrade pip
"$SMOKE_VENV/bin/python" -m pip install --quiet "$WHEEL" \
  || die "could not install the wheel - check Requires-Dist in the metadata"
ok "wheel installed with its declared dependencies only"

"$SMOKE_VENV/bin/python" - <<'PY' || die "import smoke test failed"
import pathlib
import tranchepay

print(f"     imported tranchepay {tranchepay.__version__} from {tranchepay.__file__}")
assert pathlib.Path(tranchepay.__file__).parent.joinpath("py.typed").is_file(), "py.typed missing"
print(f"     estimate_tranche_count(450000) = {tranchepay.estimate_tranche_count(450_000)}")
PY
info "python -c \"import tranchepay; print(tranchepay.__version__)\""
"$SMOKE_VENV/bin/python" -c "import tranchepay; print(tranchepay.__version__)" \
  || die "version print failed"
"$SMOKE_VENV/bin/python" -m pip check \
  || die "installed dependencies are inconsistent (pip check)"
ok "import, py.typed and dependency consistency verified"

# ==========================================================================
step "Summary and TestPyPI dry run"
# ==========================================================================
printf '\n%s%s is ready to publish.%s\n' "$C_BOLD$C_GREEN" "$PACKAGE $VERSION" "$C_RESET"
printf '   %s\n' "$SDIST"
printf '   %s\n\n' "$WHEEL"

cat <<EOF
Dry-run the upload against TestPyPI first (never your first real upload):

  # One-time: create a TestPyPI account and token at https://test.pypi.org/manage/account/token/
  export TWINE_USERNAME=__token__
  export TWINE_PASSWORD=pypi-<your TestPyPI token>

  twine upload --repository testpypi dist/*

Then prove the uploaded files install from TestPyPI as a stranger would:

  python -m pip install --index-url https://test.pypi.org/simple/ \\
      --extra-index-url https://pypi.org/simple/ tranchepay==$VERSION

Only when that works, publish for real:

  twine upload dist/*
EOF

if [[ "$UPLOAD_TESTPYPI" -eq 1 ]]; then
  printf '\n'
  info "uploading to TestPyPI..."
  if [[ -z "${TWINE_PASSWORD:-}" ]]; then
    die "TWINE_PASSWORD is not set; export a TestPyPI token or configure ~/.pypirc"
  fi
  "$BUILD_VENV/bin/python" -m twine upload --repository testpypi dist/* \
    || die "TestPyPI upload failed (a version cannot be reused - bump it and retry)"
  ok "uploaded to TestPyPI; verify with the pip command above"
else
  info "skipping the TestPyPI upload (pass --upload-testpypi to run it)"
fi
