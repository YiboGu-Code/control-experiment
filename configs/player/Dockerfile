ARG GODOT_IMAGE=gamecraft-godot:4.5.1
FROM ${GODOT_IMAGE}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 xvfb xauth libgl1-mesa-dri libxkbcommon0 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY player_driver.py player_harness.gd /opt/gamecraft/
RUN chmod 555 /opt/gamecraft /opt/gamecraft/player_driver.py /opt/gamecraft/player_harness.gd
ENV LIBGL_ALWAYS_SOFTWARE=1
USER 65534:65534
WORKDIR /output
