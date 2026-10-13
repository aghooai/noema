# noema-worker: the jobs and media-worker processes (spec-01 §4). ADR-002 §8: they run the api image's exact venv
# (no media parsers), so this image only derives from noema-api and changes the default command; uid 10001:10002 is
# the uid sandboxd accepts on the spool socket (ADR-002 §6).
# Build after noema-api:  docker build -f deploy/docker/worker.Dockerfile --build-arg NOEMA_API_IMAGE=noema-api:dev \
#                           -t noema-worker:dev .
# media-worker overrides the command:  python -m noema.entrypoints.media_worker

ARG NOEMA_API_IMAGE=noema-api:dev
FROM ${NOEMA_API_IMAGE} AS noema-worker
LABEL org.opencontainers.image.title="noema-worker" \
      org.opencontainers.image.description="Noema jobs and media-worker processes (the noema-api venv, no media parsers)"
USER 10001:10002
EXPOSE 9464
STOPSIGNAL SIGTERM
CMD ["python", "-m", "noema.entrypoints.jobs", "--kinds", "labeling.*,session.delete,retention.sweep"]
