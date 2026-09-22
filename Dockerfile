# One image, two roles: `agent` (a pipeline) and `serve` (the UI).
#
# Everything the bare-metal install asks you to do by hand -- create a service
# account, chown the staging tree, fetch and chmod the Proton binary, install
# PyYAML, copy units, get file modes right -- is either baked in here or is a
# single line in docker-compose.yml.
#
#   docker compose build
#   docker compose run --rm david login      # once per pipeline
#   docker compose up -d
#
# The Proton CLI is a ~113 MB Bun binary, which is most of the image. It is
# pinned: the CLI's flags drift between releases (0.8.0 rejects the `-c` that
# 0.6.0 documents), and a container that silently upgrades it would break
# downloads at 03:15 rather than when you were watching.

FROM python:3.12-slim-bookworm AS base

# 0.8.0 is the version this project has actually been run against.
ARG PROTON_DRIVE_VERSION=0.8.0
# linux-x64 needs AVX2. On a CPU without it -- `grep avx2 /proc/cpuinfo` comes
# back empty -- build with:
#   docker compose build --build-arg PROTON_DRIVE_ARCH=linux-x64-baseline
ARG PROTON_DRIVE_ARCH=linux-x64
ARG PROTON_DRIVE_URL=https://proton.me/download/drive/cli/${PROTON_DRIVE_VERSION}/${PROTON_DRIVE_ARCH}/proton-drive

# ca-certificates for TLS to Proton and Immich; curl only to fetch the binary.
# PyYAML is the one optional dependency the project has, and it is what makes
# an `accounts:` list readable -- in an image there is no reason to go without.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir "PyYAML==6.0.2"

# chmod 755 explicitly: a download arrives 644 and exec then fails even for
# root, which is the first-boot failure the bare-metal install warns about.
# `--version` is the build-time proof that the right binary for this CPU
# landed -- a baseline/AVX2 mismatch fails here rather than at 03:15.
RUN curl -fsSL "${PROTON_DRIVE_URL}" -o /usr/local/bin/proton-drive \
 && chmod 755 /usr/local/bin/proton-drive \
 && /usr/local/bin/proton-drive --version

WORKDIR /app
COPY sync.py ./
COPY src/ ./src/
COPY web/ ./web/

# Not root. The uid must own the staging and state volumes on the host, so it
# is overridable at build time -- match it to whoever owns /mnt/immich/staging.
ARG UID=1000
ARG GID=1000
RUN groupadd -g "${GID}" pis 2>/dev/null || true \
 && useradd -u "${UID}" -g "${GID}" -M -d /app -s /usr/sbin/nologin pis 2>/dev/null || true \
 && chown -R "${UID}:${GID}" /app
USER ${UID}:${GID}

# Set by compose per service. PIS_CONFIG points at the mounted config, and
# PIS_ACCOUNT selects which account in it this container is.
ENV PIS_CONFIG=/config/config.yaml \
    PYTHONUNBUFFERED=1 \
    PROTON_DRIVE_CREDENTIALS_STORE=unsafe_file

# The pipeline role. `serve` overrides this in compose.
ENTRYPOINT ["python3", "/app/sync.py"]
CMD ["agent"]
