# Skill Test Generator World
FROM --platform=linux/amd64 383806609161.dkr.ecr.us-west-1.amazonaws.com/vm/rootfs/plato-world-base:latest

# Install Node.js 20 LTS (needed for next start in production mode; bun has compat issues)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

# Install bun (used by variant runner for building generated apps)
RUN curl -fsSL https://bun.sh/install | bash && \
    ln -sf /root/.bun/bin/bun /usr/local/bin/bun && \
    ln -sf /root/.bun/bin/bunx /usr/local/bin/bunx

# Bundle the sohan template
COPY templates/sohan /world/templates/sohan

# Install world package + deps into the venv
COPY pyproject.toml hatch_build.py /world/
COPY src /world/src
RUN uv pip install --python /opt/plato-venv/bin/python --no-sources /world
