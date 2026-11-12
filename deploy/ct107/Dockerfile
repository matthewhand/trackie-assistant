ARG RUNTIME_BASE=cgr.dev/chainguard/wolfi-base:latest
FROM ${RUNTIME_BASE}

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

# Wolfi supplies maintained glibc, Python 3.13 and FFmpeg 9 packages.
# The virtual environment keeps pip-managed application files separate from APK.
RUN apk add --no-cache python-3.13 py3.13-pip bash ca-certificates ffmpeg-9.0 shadow
RUN /usr/bin/python3.13 -m venv --without-pip /opt/venv \
    && /usr/bin/python3.13 -m pip --python /opt/venv/bin/python install --no-cache-dir --upgrade 'pip>=26.3' \
    && apk del py3.13-pip
RUN python -c 'import sys; assert sys.version_info[:2] == (3, 13), sys.version' \
    && ffmpeg -version && ffprobe -version

# Must match the one wheel supplied in incoming/. Compose requires an explicit
# reviewed image tag and expected package version; no stale release is selected.
ARG CLI_VERSION
ARG INSTALL_BROWSER=0
RUN test -n "$CLI_VERSION" || { echo 'CLI_VERSION build argument is required' >&2; exit 1; }

COPY incoming/ /tmp/incoming/
RUN set -eu; \
    wheels=$(find /tmp/incoming -maxdepth 1 -name '*.whl' -type f | wc -l); \
    if [ "$wheels" -ne 1 ]; then \
        echo "expected exactly one wheel in deploy/ct107/incoming/, found $wheels" >&2; \
        exit 1; \
    fi; \
    if [ "$INSTALL_BROWSER" = "1" ]; then \
        apk add --no-cache libatomic libnspr libnss libatk-1.0 libatk-bridge-2.0 libxfixes libudev at-spi2-core libxcomposite libxdamage libxrandr mesa-gbm alsa-lib pango libxkbcommon fontconfig ttf-dejavu; \
        pip install --no-cache-dir /tmp/incoming/*.whl 'mutagen>=1.47.0' 'playwright>=1.42.0'; \
        playwright install --only-shell chromium; \
        python -c "from pathlib import Path; import shutil; [shutil.rmtree(p) for p in Path('/opt/playwright').glob('ffmpeg-*') if p.is_dir() and not p.is_symlink()]"; \
        chmod -R a+rX /opt/playwright; \
    else \
        pip install --no-cache-dir /tmp/incoming/*.whl 'mutagen>=1.47.0'; \
    fi; \
    rm -rf /tmp/incoming; \
    installed=$(python -c 'import suno_dl; print(suno_dl.__version__)'); \
    if [ "$installed" != "$CLI_VERSION" ]; then \
        echo "wheel version $installed does not match CLI_VERSION $CLI_VERSION" >&2; \
        exit 1; \
    fi

COPY gateway-entrypoint.sh worker-entrypoint.sh healthcheck.py /usr/local/bin/
RUN chmod 0755 /usr/local/bin/gateway-entrypoint.sh \
        /usr/local/bin/worker-entrypoint.sh /usr/local/bin/healthcheck.py \
    && ln -s /usr/local/bin/gateway-entrypoint.sh /usr/local/bin/ct107-gateway \
    && ln -s /usr/local/bin/worker-entrypoint.sh /usr/local/bin/ct107-worker \
    && ln -s /usr/local/bin/healthcheck.py /usr/local/bin/ct107-healthcheck \
    && groupadd --gid 10001 trackie \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /sbin/nologin trackie
USER 10001:10001
WORKDIR /data/archive
