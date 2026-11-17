# The official Music Assistant server image of a release, with the feature commits of this
# fork's release branch and the light-follow frontend wheel laid over it. Everything else
# (dependencies, ffmpeg, cliairplay, the bundled app credentials) stays as the official image
# ships it. The image reports itself as an unofficial build.
ARG SERVER_TAG
FROM ghcr.io/music-assistant/server:${SERVER_TAG}

COPY context/ /tmp/lights/
RUN python /tmp/lights/apply_overlay.py /tmp/lights && rm -rf /tmp/lights

ARG SERVER_TAG
ARG SOURCE_URL
ARG BUILD_KEY
LABEL \
    org.opencontainers.image.title="Music Assistant Server (light follow)" \
    org.opencontainers.image.description="Music Assistant server ${SERVER_TAG} with Hue lights that follow a speaker. Unofficial build." \
    org.opencontainers.image.source="${SOURCE_URL}" \
    org.opencontainers.image.version="${SERVER_TAG}" \
    io.github.fork-sync.build-key="${BUILD_KEY}"
