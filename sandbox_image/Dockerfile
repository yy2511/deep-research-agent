FROM node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS node-runtime

FROM python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b AS dependencies
COPY --from=ghcr.io/astral-sh/uv:0.9.5@sha256:f459f6f73a8c4ef5d69f4e6fbbdb8af751d6fa40ec34b39a1ab469acd6e289b7 /uv /bin/uv
WORKDIR /opt/runtime
COPY pyproject.toml uv.lock ./
RUN UV_PYTHON_DOWNLOADS=0 UV_HTTP_TIMEOUT=120 uv sync --locked --no-dev --no-install-project

FROM python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b
LABEL dra.sandbox.protocol="3" dra.sandbox.dependencies="1"
LABEL dra.sandbox.tools="curl,wget,file,git,zip,unzip,tar,xz,jq,rg,node,npm,pdftotext,pdftoppm,convert,cc,make"
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 graphviz fonts-noto-cjk \
    && mkdir -p /workspace/input /workspace/work /workspace/output /opt/dra-tools \
    && dpkg-query -W > /opt/dra-tools/system-packages.txt \
    && rm -rf /var/lib/apt/lists/*
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl wget file git zip unzip tar xz-utils jq ripgrep poppler-utils imagemagick build-essential \
    && dpkg-query -W > /opt/dra-tools/system-packages.txt \
    && rm -rf /var/lib/apt/lists/*
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
COPY --from=dependencies /opt/runtime/.venv /opt/runtime/.venv
RUN /opt/runtime/.venv/bin/python -c 'import importlib.metadata as m; print("\n".join(sorted(d.metadata["Name"]+"=="+d.version for d in m.distributions())))' > /opt/dra-tools/python-packages.txt
COPY helper.py lifetime.py /opt/dra-tools/
COPY --chmod=755 render.py /usr/local/bin/dra-render
ENV PATH=/opt/runtime/.venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LANG=C.UTF-8 HOME=/tmp
USER 1001:1001
WORKDIR /
ENTRYPOINT ["python", "-I", "/opt/dra-tools/lifetime.py"]
