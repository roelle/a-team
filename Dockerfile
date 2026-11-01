# a-team in a container, on an API key. The agy CLI runtime needs its own
# install and sign-in, so it is not in this image.
#
#   docker build -t a-team .
#   docker run -p 127.0.0.1:8765:8765 -v a-team:/data -e GEMINI_API_KEY a-team
#
# Open the URL it prints, which has the token. Works the same with podman.
FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/a-team
COPY pyproject.toml README.md ./
COPY ateam ateam
COPY plugin plugin
RUN pip install --no-cache-dir ".[sdk,claude]"

# Agents run commands in here, so not as root.
RUN useradd --create-home --uid 1000 ateam && mkdir /data && chown ateam /data
USER ateam
WORKDIR /data
ENV ATEAM_TEAMS_ROOT=/data/teams
VOLUME /data

EXPOSE 8765
# Beyond loopback inside the container only: publish it to 127.0.0.1 as above.
CMD ["a-team", "--host", "0.0.0.0", "--no-open", "--start-teams", "--author", "operator"]
