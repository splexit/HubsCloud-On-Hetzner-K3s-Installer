FROM node:22-bookworm AS ui-build
WORKDIR /build
COPY app/ui/package.json app/ui/package-lock.json* ./
# Skip native postinstall scripts during npm ci to avoid esbuild ETXTBSY on Docker Desktop (Mac).
# Vite still works via esbuild's JS fallback; native binary install is best-effort.
RUN npm ci --ignore-scripts \
    && (node node_modules/esbuild/install.js || true)
COPY app/ui/ ./
RUN npm run build

FROM python:3.12-slim-bookworm

ARG TARGETARCH
ENV DEBIAN_FRONTEND=noninteractive
ENV TEMPLATES_DIR=/opt/templates
ENV DATA_DIR=/data
ENV INSTALLER_README=/opt/installer/README.md
ENV APP_VERSION=1.2.0
ENV PYTHONPATH=/app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl wget openssh-client git ca-certificates unzip \
    && rm -rf /var/lib/apt/lists/*

RUN ARCH="${TARGETARCH:-amd64}" \
    && TF_VER=1.9.8 \
    && curl -fsSL "https://releases.hashicorp.com/terraform/${TF_VER}/terraform_${TF_VER}_linux_${ARCH}.zip" -o /tmp/terraform.zip \
    && unzip /tmp/terraform.zip -d /usr/local/bin \
    && rm /tmp/terraform.zip \
    && ARCH="${TARGETARCH:-amd64}" \
    && KVER=$(curl -fsSL https://dl.k8s.io/release/stable.txt) \
    && curl -fsSL "https://dl.k8s.io/release/${KVER}/bin/linux/${ARCH}/kubectl" -o /usr/local/bin/kubectl \
    && chmod +x /usr/local/bin/kubectl \
    && curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

WORKDIR /app
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ /app/
COPY --from=ui-build /build/dist /app/ui/dist
COPY templates/ /opt/templates/
COPY README.md /opt/installer/README.md
COPY LICENSE /opt/installer/LICENSE
COPY AI-DISCLOSURE.md /opt/installer/AI-DISCLOSURE.md
COPY THIRD-PARTY-NOTICES.md /opt/installer/THIRD-PARTY-NOTICES.md

RUN mkdir -p /data && chmod 777 /data

EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "info"]
