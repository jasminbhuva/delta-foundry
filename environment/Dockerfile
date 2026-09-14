# Delta Foundry agent image. Everything here is visible to the agent; nothing
# references the sealed verifier.
FROM debian:trixie@sha256:f324c7ff54321e8d9c588493a20244965938ce0aa50bbd1022d38010e9ffc4b1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential zlib1g-dev python3 xz-utils ca-certificates curl git perl \
      util-linux procps time less file gdb valgrind linux-perf libasan8 libubsan1 \
 && rm -rf /var/lib/apt/lists/*

# Pinned upstream source: Git v2.55.0 (commit e9019fcafe0040228b8631c30f97ae1adb61bcdc).
COPY public/git-2.55.0.tar.xz /tmp/git-2.55.0.tar.xz
RUN echo '457fdb04dc8728e007d4688695e6912e6f680727920f2a40bf11eacc17505357  /tmp/git-2.55.0.tar.xz' | sha256sum -c - \
 && mkdir -p /opt/delta-foundry/pristine-src /app/git \
 && tar -xJf /tmp/git-2.55.0.tar.xz -C /opt/delta-foundry/pristine-src --strip-components=1 \
 && tar -xJf /tmp/git-2.55.0.tar.xz -C /app/git --strip-components=1 \
 && rm /tmp/git-2.55.0.tar.xz

COPY ci/ /app/ci/
COPY public/ /opt/delta-foundry/public/

# Install a pristine Git to /usr/local (baseline and oracle for the self-check;
# its helpers make case materialization self-contained), prebuild a pristine
# tree in /app/build so /app/ci/build.sh is incremental, and materialize the
# public cases. /usr/local git is the agent's own reference; the sealed verifier
# uses its own image, so the agent cannot affect scoring by touching it.
RUN groupadd -g 61011 dfenc && useradd -M -d /nonexistent -s /usr/sbin/nologin -u 61011 -g 61011 dfenc \
 && groupadd -g 61012 dforacle && useradd -M -d /nonexistent -s /usr/sbin/nologin -u 61012 -g 61012 dforacle \
 && cp -a /opt/delta-foundry/pristine-src /tmp/pristine-build \
 && python3 /app/ci/build.py --install-pristine /tmp/pristine-build \
 && rm -rf /tmp/pristine-build \
 && cp -a /opt/delta-foundry/pristine-src /app/build \
 && cd /app/build && python3 /app/ci/build.py --prebuild \
 && python3 /app/ci/gen_cases.py /usr/local/bin/git /app/ci/public-workloads.json \
      /opt/delta-foundry/public/excerpts /opt/delta-foundry/public-cases \
 && chmod -R a-w,go+rX /opt/delta-foundry/public-cases /opt/delta-foundry/pristine-src \
 && chmod 0755 /opt/delta-foundry /opt/delta-foundry/public-cases \
 && chmod +x /app/ci/*.sh

WORKDIR /app
