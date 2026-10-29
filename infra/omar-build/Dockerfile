# What OMAR's release workflow builds on (.github/workflows/release.yml): Ubuntu 22.04, so
# the binary loads wherever a release's does (glibc >= 2.35), Rust, Lean through elan
# (omarc's own lean-toolchain picks the version), and Node 22 for Mission Control.
FROM ubuntu:22.04
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git build-essential \
      pkg-config libssl-dev tmux xz-utils \
 && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable \
 && curl -fsSL https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh \
      | sh -s -- -y --default-toolchain none \
 && curl -fsSL https://nodejs.org/dist/v22.13.1/node-v22.13.1-linux-x64.tar.xz | tar xJ -C /opt
ENV PATH=/root/.cargo/bin:/root/.elan/bin:/opt/node-v22.13.1-linux-x64/bin:$PATH
