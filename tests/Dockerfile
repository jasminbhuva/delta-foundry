# Delta Foundry sealed verifier image (Harbor separate verifier mode).
# Nothing in this image is visible to the agent.
FROM debian:trixie@sha256:f324c7ff54321e8d9c588493a20244965938ce0aa50bbd1022d38010e9ffc4b1

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential zlib1g-dev python3 xz-utils util-linux procps \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd -g 61010 dfbuild && useradd -M -d /nonexistent -s /usr/sbin/nologin -u 61010 -g 61010 dfbuild \
 && groupadd -g 61011 dfenc && useradd -M -d /nonexistent -s /usr/sbin/nologin -u 61011 -g 61011 dfenc \
 && groupadd -g 61012 dforacle && useradd -M -d /nonexistent -s /usr/sbin/nologin -u 61012 -g 61012 dforacle

COPY git-2.55.0.tar.xz /tmp/git-2.55.0.tar.xz
RUN echo '457fdb04dc8728e007d4688695e6912e6f680727920f2a40bf11eacc17505357  /tmp/git-2.55.0.tar.xz' | sha256sum -c - \
 && mkdir -p /opt/df/pristine-src /work \
 && tar -xJf /tmp/git-2.55.0.tar.xz -C /opt/df/pristine-src --strip-components=1 \
 && rm /tmp/git-2.55.0.tar.xz

COPY supervisor.py reward_guard.py gen_cases.py build_verifier.py workloads.json /opt/df/verifier/
COPY dfcore/ /opt/df/verifier/dfcore/
COPY fixtures/ /opt/df/sealed/excerpts/

# Pristine baseline/oracle binaries, the prebuilt pristine candidate tree owned
# by the unprivileged build user, and the sealed cases (materialized now; the
# verifier fetches nothing).
RUN python3 /opt/df/verifier/build_verifier.py \
 && chmod 0700 /opt/df/verifier /opt/df/sealed /opt/df/pristine-src

COPY test.sh /tests/test.sh
RUN chmod 0700 /tests && chmod 0755 /tests/test.sh
WORKDIR /app
