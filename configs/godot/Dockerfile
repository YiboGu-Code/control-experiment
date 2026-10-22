FROM debian:bookworm-slim

# Host-built validation image. No model-generated code runs while building it.
ARG GODOT_VERSION=4.5.1
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl unzip libfontconfig1 libx11-6 libxcursor1 libxinerama1 \
    libxrandr2 libxi6 libgl1 libasound2 libpulse0 libvulkan1 \
    && rm -rf /var/lib/apt/lists/*
RUN cd /tmp \
    && curl --fail --location --silent --show-error \
      "https://github.com/godotengine/godot/releases/download/${GODOT_VERSION}-stable/Godot_v${GODOT_VERSION}-stable_linux.x86_64.zip" -o godot.zip \
    && unzip -q godot.zip \
    && install -m 755 "Godot_v${GODOT_VERSION}-stable_linux.x86_64" /usr/local/bin/godot \
    && rm godot.zip "Godot_v${GODOT_VERSION}-stable_linux.x86_64"
RUN godot --headless --version
WORKDIR /output
USER 65534:65534
