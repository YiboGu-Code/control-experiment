FROM node:22.21.1-bookworm-slim AS dependencies
WORKDIR /opt/godot-mcp
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --omit=dev --no-audit --no-fund

FROM gamecraft-player:4.5.1
USER root
COPY --from=dependencies /usr/local/bin/node /usr/local/bin/node
COPY --from=dependencies /opt/godot-mcp /opt/godot-mcp
COPY mcp_driver.py godot_wrapper.py /opt/gamecraft/
RUN chmod 555 /opt/gamecraft/mcp_driver.py /opt/gamecraft/godot_wrapper.py
ENV GODOT_PATH=/opt/gamecraft/godot_wrapper.py GODOT_MCP_STRICT=true
USER 65534:65534
WORKDIR /output
