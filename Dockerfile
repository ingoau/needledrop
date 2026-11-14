FROM python:3.13-slim-trixie

ARG FREAC_VERSION=1.1.7
ARG FREAC_SHA256=
# The pip that ships with the base image vendors an old urllib3 and msgpack.
ARG PIP_VERSION=26.3

# ffmpeg/ffprobe for transcoding, fpcalc for beets' chroma plugin, and the
# GTK/codec runtime fre:ac's command line tool links against (even headless).
# The upgrade picks up Debian security fixes newer than the base image.
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl git tini ffmpeg libchromaprint-tools \
        libgtk-3-0t64 libpulse0 libsndfile1 libflac14 libogg0 libvorbis0a \
        libvorbisenc2 libopus0 libmp3lame0 libmpg123-0t64 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --root-user-action=ignore "pip==${PIP_VERSION}"

# fre:ac: decodes Apple's ALAC more reliably than FFmpeg does
RUN curl -fsSL -o /tmp/freac.AppImage \
        "https://github.com/enzo1982/freac/releases/download/v${FREAC_VERSION}/freac-${FREAC_VERSION}-linux-x86_64.AppImage" \
    && if [ -n "$FREAC_SHA256" ]; then echo "$FREAC_SHA256  /tmp/freac.AppImage" | sha256sum -c -; fi \
    && chmod +x /tmp/freac.AppImage && cd /opt && /tmp/freac.AppImage --appimage-extract >/dev/null \
    && mv squashfs-root freac && rm /tmp/freac.AppImage \
    && chmod -R a+rX /opt/freac \
    && ln -s /opt/freac/AppRun /usr/local/bin/freaccmd \
    && freaccmd --help=flac | grep -q "FLAC Audio Encoder"

# beets, in its own venv, at pinned versions
COPY beets/requirements.txt /tmp/beets-requirements.txt
RUN python -m venv /opt/beets \
    && /opt/beets/bin/pip install --no-cache-dir "pip==${PIP_VERSION}" \
    && /opt/beets/bin/pip install --no-cache-dir -r /tmp/beets-requirements.txt

# gamdl + the web app
COPY requirements.txt /tmp/app-requirements.txt
RUN python -m venv /opt/app \
    && /opt/app/bin/pip install --no-cache-dir "pip==${PIP_VERSION}" \
    && /opt/app/bin/pip install --no-cache-dir -r /tmp/app-requirements.txt

COPY --chmod=a+rX beets /opt/needledrop/beets
COPY --chmod=a+rX app /opt/needledrop/app

ENV PYTHONUNBUFFERED=1 \
    PATH=/opt/app/bin:$PATH \
    FREACCMD=/usr/local/bin/freaccmd \
    NEEDLEDROP_DATA=/data \
    MUSIC_DIR=/music \
    BEETSDIR=/config/beets \
    HOME=/data

WORKDIR /opt/needledrop
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
