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

# Chromium runtime libraries are APK-managed; Playwright supplies its matched
# headless browser. No obsolete system Chromium or unused recorder is shipped.
RUN apk add --no-cache libatomic nss at-spi2-core libxcomposite libxdamage libxrandr mesa-gbm alsa-lib pango libxkbcommon fontconfig ttf-dejavu

COPY trackie_assistant-*.whl /tmp/
RUN pip install --no-cache-dir /tmp/trackie_assistant-*.whl 'playwright>=1.42.0' 'mutagen>=1.47.0' \
    && rm -f /tmp/trackie_assistant-*.whl \
    && playwright install --only-shell chromium \
    && python -c "from pathlib import Path; import shutil; [shutil.rmtree(p) for p in Path('/opt/playwright').glob('ffmpeg-*') if p.is_dir() and not p.is_symlink()]" \
    && chmod -R a+rX /opt/playwright

# Libraries Chromium loads dynamically are split into these Wolfi packages.
RUN apk add --no-cache libnspr libnss libatk-1.0 libatk-bridge-2.0 libxfixes libudev

COPY run.sh /run.sh
RUN chmod +x /run.sh
WORKDIR /data/archive
CMD ["/run.sh"]
