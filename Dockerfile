# Image du serveur MCP, publiée sur ghcr.io/nic01asfr/sspcloud-mcp.
# Le pod Onyxia tire cette image : pas de pip au démarrage.
# kubectl + helm sont dans l'image : le serveur lance les pods de travail.
FROM python:3.11-slim-bookworm

ARG KUBECTL_VERSION=v1.31.4
ARG HELM_VERSION=v3.16.4

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git \
 && curl -fsSL -o /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
 && chmod 755 /usr/local/bin/kubectl \
 && curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" \
      | tar -xz -C /tmp \
 && mv /tmp/linux-amd64/helm /usr/local/bin/helm \
 && chmod 755 /usr/local/bin/helm \
 && rm -rf /tmp/linux-amd64 /var/lib/apt/lists/* \
 && useradd --uid 1000 --create-home --shell /bin/bash mcp

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY sspcloud_mcp ./sspcloud_mcp
RUN pip install --no-cache-dir .

USER 1000
ENV PORT=8000 \
    HOME=/home/mcp
EXPOSE 8000
CMD ["python", "-m", "sspcloud_mcp.server_http"]
