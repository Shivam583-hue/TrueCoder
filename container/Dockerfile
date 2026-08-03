# syntax=docker/dockerfile:1

FROM python@sha256:399babc8b49529dabfd9c922f2b5eea81d611e4512e3ed250d75bd2e7683f4b0 AS base

FROM base AS runtime

ARG EXEC_UID=65532
ARG EXEC_GID=65532

RUN addgroup -g "${EXEC_GID}" -S truecoder \
 && adduser -u "${EXEC_UID}" -G truecoder -S -D -H -s /sbin/nologin truecoder \
 && mkdir -p /workspace /home/truecoder \
 && chown "${EXEC_UID}:${EXEC_GID}" /home/truecoder \
 && rm -rf /root/.cache /tmp/*

COPY --chown=root:root entrypoint.py /opt/truecoder/entrypoint.py
RUN chmod 0555 /opt/truecoder/entrypoint.py

ENV PATH=/usr/local/bin:/usr/bin:/bin \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/truecoder \
    TRUECODER_ENTRYPOINT_VERSION=1

LABEL ai.truecoder.image=execution \
      ai.truecoder.entrypoint-version=1 \
      ai.truecoder.user=65532:65532

USER 65532:65532
WORKDIR /workspace

ENTRYPOINT ["/usr/local/bin/python3", "/opt/truecoder/entrypoint.py"]
