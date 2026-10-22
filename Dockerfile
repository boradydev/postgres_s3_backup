# syntax=docker/dockerfile:1
ARG POSTGRES_IMAGE=postgres:17-bookworm@sha256:858bb5e98eed02eb8cafbcdc007f9a65d1b613972028944a8ec2133ccaf80d27
FROM ${POSTGRES_IMAGE} AS backup
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/backup-venv
COPY requirements.txt /opt/backup-requirements.txt
RUN /opt/backup-venv/bin/pip install --no-cache-dir --require-hashes -r /opt/backup-requirements.txt \
    && groupadd --gid 10001 mfbackup && useradd --uid 10001 --gid mfbackup --no-create-home mfbackup \
    && mkdir /state && chown mfbackup:mfbackup /state
ENV PATH="/opt/backup-venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY src/backup_service.py /opt/backup/runner.py
USER mfbackup
ENTRYPOINT ["python", "/opt/backup/runner.py"]
CMD ["serve"]

FROM backup AS test
USER root
COPY requirements-dev.txt /opt/requirements-dev.txt
RUN pip install --no-cache-dir --require-hashes -r /opt/requirements-dev.txt
WORKDIR /app
COPY --chown=mfbackup:mfbackup src ./src
COPY --chown=mfbackup:mfbackup tests ./tests
COPY --chown=mfbackup:mfbackup pyproject.toml ./
RUN chown mfbackup:mfbackup /app
USER mfbackup
ENTRYPOINT ["python", "-m", "pytest"]
CMD ["-q"]

FROM backup AS runtime
