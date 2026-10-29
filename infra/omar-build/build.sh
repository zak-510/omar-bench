#!/bin/sh
# Builds OMAR at one commit, as its release workflow builds a tag, into a tarball laid out
# like a release's: omar-linux-amd64/{omar,omarc}. For an arm on a commit no release has.
#
#   infra/omar-build/build.sh COMMIT      (on an amd64 machine with Docker)
#
# Writes vendor/omar/omar-<short>-linux-amd64.tar.gz, the language reference at that commit
# (omar-<short>-spec.md) and what the build ran on (omar-<short>-build.txt), then prints
# the tarball's sha256 for bench.toml.
set -eu
: "${1:?give the commit to build}"
cd "$(dirname "$0")/../.."
commit=$1
short=$(printf %s "$commit" | cut -c1-7)
out=$PWD/vendor/omar
mkdir -p "$out"
docker build -q -t omar-bench-omar-build infra/omar-build > /dev/null
# A quarter of the CPU weight of anything else on the machine: a queue's task may share it.
docker run --rm --cpu-shares 256 -v "$out:/out" -e COMMIT="$commit" -e SHORT="$short" \
  omar-bench-omar-build sh -euc '
  git clone -q https://github.com/omar-os/omar /src
  cd /src
  git checkout -q "$COMMIT"
  (cd lang && lake build omarc)
  (cd web && npm ci --no-audit --no-fund && npm run build:spa)
  cargo build --release --features ui
  # The release workflow checks a direct run before it packages one; so does this.
  OMARC_BIN=$PWD/lang/.lake/build/bin/omarc target/release/omar run tests/ci/direct_run.omar \
    --input run.request=hello --replace --fg | grep -Fq "Output run.result = \"hello\""
  mkdir -p /tmp/pack/omar-linux-amd64
  cp target/release/omar lang/.lake/build/bin/omarc /tmp/pack/omar-linux-amd64/
  tar czf "/out/omar-$SHORT-linux-amd64.tar.gz" -C /tmp/pack omar-linux-amd64
  cp lang/spec.md "/out/omar-$SHORT-spec.md"
  { git rev-parse HEAD; rustc --version; (cd lang && lean --version); node --version
    target/release/omar --version; } > "/out/omar-$SHORT-build.txt"
'
sha256sum "$out/omar-$short-linux-amd64.tar.gz"
