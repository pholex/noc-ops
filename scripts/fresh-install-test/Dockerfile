# 「全新 Linux 机器」安装测试镜像：模拟一个没装任何云 CLI、没有 ~/.ssh 的新用户按 INSTALL.md 走一遍。
# 由 scripts/fresh-install-test/run.sh 构建并运行，不要直接 docker build（需要先把 wheel 放进 context）。
FROM python:3.12-slim-bookworm
ENV DEBIAN_FRONTEND=noninteractive PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates git nodejs npm procps \
    && rm -rf /var/lib/apt/lists/*
RUN useradd -m -s /bin/bash tester
USER tester
WORKDIR /home/tester
ENV PATH="/home/tester/.local/bin:${PATH}" HOME=/home/tester
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
COPY --chown=tester noc_ops-*.whl /home/tester/
COPY --chown=tester run_install_test.sh /home/tester/run_install_test.sh
CMD ["bash", "/home/tester/run_install_test.sh"]
