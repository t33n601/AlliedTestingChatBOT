#!/usr/bin/env bash
# Boots Selenoid (browsers in Docker) on :4445 and its dashboard on :8083.
set -e
cd "$(dirname "$0")"
mkdir -p video logs

docker pull selenoid/vnc_chrome:128.0
docker pull selenoid/video-recorder:latest-release

docker rm -f kb_selenoid kb_selenoid_ui >/dev/null 2>&1 || true

docker run -d --name kb_selenoid \
  -p 4445:4444 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD":/etc/selenoid:ro \
  -v "$PWD/video":/opt/selenoid/video \
  -v "$PWD/logs":/opt/selenoid/logs \
  -e OVERRIDE_VIDEO_OUTPUT_DIR="$PWD/video" \
  -e DOCKER_API_VERSION=1.44 \
  aerokube/selenoid:latest-release \
  -conf /etc/selenoid/browsers.json \
  -video-output-dir /opt/selenoid/video \
  -log-output-dir /opt/selenoid/logs

docker run -d --name kb_selenoid_ui \
  -p 8083:8080 \
  --link kb_selenoid:selenoid \
  aerokube/selenoid-ui:latest-release --selenoid-uri http://selenoid:4444

echo "Selenoid:  http://localhost:4445/wd/hub"
echo "Dashboard: http://localhost:8083"
