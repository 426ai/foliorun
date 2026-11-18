FROM python:3.12.16-slim-trixie@sha256:05a994d2344eb364cc186910f4fc8786a38215f39f00d0acc25bff33a08f4177
USER root
RUN useradd --uid 10001 --create-home app
COPY image/os-packages.lock image/requirements.lock /opt/document-tools/
RUN rm -f /etc/apt/sources.list /etc/apt/sources.list.d/* \
    && printf '%s\n' \
      'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20261115T000000Z/ trixie main' \
      'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/20261115T000000Z/ trixie-security main' \
      > /etc/apt/sources.list \
    && apt-get update \
    && xargs apt-get install -y --no-install-recommends < /opt/document-tools/os-packages.lock \
    && python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r /opt/document-tools/requirements.lock \
    && dpkg-query -W -f='${Package}\t${Version}\t${Architecture}\n' > /opt/document-tools/dpkg-inventory.tsv \
    && python -m pip list --format=json > /opt/document-tools/python-inventory.json \
    && rm -rf /var/lib/apt/lists/*
COPY image/tool.py image/THIRD_PARTY_NOTICES.md /opt/document-tools/
COPY skills /skills
COPY image/skills-manifest.json /opt/document-tools/skills-manifest.json
RUN chmod -R a-w /skills /opt/document-tools \
    && mkdir -p /workspace /output /previous \
    && chown app:app /workspace /output
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /workspace
CMD ["sleep", "infinity"]
