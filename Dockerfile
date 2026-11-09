# =================================
# Dockerfile for trackfs
# 
# Copyright 2020 by Andreas Schmidt
# All rights reserved.
# This file is part of the trackfs project
# and licensed under the terms of the GNU Lesser General Public License v3.0.
# See https://github.com/andresch/trackfs for details.
#
# =================================
# syntax=docker/dockerfile:1

# Build FLAC 1.5.0 separately so BuildKit can reuse its installed prefix.
FROM docker.io/python:3.11-alpine AS flac-builder

ARG TARGETPLATFORM

RUN \
  apk update \
  && apk --no-cache add alpine-sdk git cmake doxygen pandoc

RUN \
    --mount=type=cache,id=flac-build-${TARGETPLATFORM},target=/tmp/flac-build,sharing=locked \
    --mount=type=cache,id=flac-cache-${TARGETPLATFORM},target=/tmp/flac-cache,sharing=locked \
    set -eux; \
    mkdir -p /tmp/flac-prefix /tmp/flac-output; \
    if [ ! -f /tmp/flac-cache/flac-1.5.0.tar.gz ]; then \
      rm -rf /tmp/flac-prefix/*; \
      if [ ! -d /tmp/flac-build/flac/.git ]; then \
        rm -rf /tmp/flac-build/flac; \
        git clone -b 1.5.0 https://github.com/xiph/flac.git /tmp/flac-build/flac; \
      fi; \
      cd /tmp/flac-build/flac; \
      if [ ! -d ogg/.git ]; then git clone https://github.com/xiph/ogg.git ogg; fi; \
      grep -q 'target_link_libraries(replaygain_analysis m)' src/share/replaygain_analysis/CMakeLists.txt \
        || printf '\ntarget_link_libraries(replaygain_analysis m)\n' >> src/share/replaygain_analysis/CMakeLists.txt; \
      cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/flac; \
      cmake --build build -j "$(nproc)"; \
      DESTDIR=/tmp/flac-prefix cmake --install build; \
      make -C build clean; \
      tar -C /tmp/flac-prefix -czf /tmp/flac-cache/flac-1.5.0.tar.gz opt/flac; \
    fi; \
    tar -C /tmp/flac-prefix -xzf /tmp/flac-cache/flac-1.5.0.tar.gz; \
    cp -a /tmp/flac-prefix/opt/flac/* /tmp/flac-output/

# Install trackfs and Python dependencies in an isolated environment.
FROM docker.io/python:3.11-alpine AS app-builder

RUN  apk add --no-cache gcc musl-dev linux-headers

WORKDIR /app
COPY pyproject.toml setup.py VERSION README.py.md ./
COPY trackfs ./trackfs

RUN \
    python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir .

# Runtime contains no compiler, Git, CMake, or documentation generators.
FROM docker.io/python:3.11-alpine AS runtime
ENV PATH="/opt/venv/bin:/opt/flac/bin:${PATH}"

RUN \
  apk add --no-cache fuse libstdc++ libgcc \
  && ln -sfn libfuse.so.2 /usr/lib/libfuse.so \
  && echo "user_allow_other" >> /etc/fuse.conf \
  && chmod 666 /etc/passwd

COPY --from=app-builder /opt/venv /opt/venv
COPY --from=flac-builder /tmp/flac-output/ /opt/flac/

COPY launcher.sh /usr/local/bin/launcher.sh
RUN chmod 555 /usr/local/bin/launcher.sh

# Source directory containing FLAC+CUE files.
VOLUME /src

# Mount point for the generated track files.
VOLUME /dst

ENTRYPOINT ["/usr/local/bin/launcher.sh"]
CMD ["-t 256", "/src", "/dst"]
