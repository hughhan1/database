#!/usr/bin/env bash
# Start an image under the restrictions Holon runs it with, and wait for it to
# answer on port 3000. The database values are placeholders: the provider reads
# them on first use, so it must start without a reachable database.
set -euo pipefail

image=$1
container=$(docker create -p 127.0.0.1::3000 \
  --read-only --user 10001:10001 --cap-drop ALL --tmpfs /tmp \
  -e DATABASE_HOST=database.invalid -e DATABASE_USER=postgres -e DATABASE_PASSWORD=password \
  "$image")
trap 'docker logs "$container"; docker rm -f "$container" >/dev/null' EXIT
docker start "$container" >/dev/null

origin="http://$(docker port "$container" 3000/tcp)"
curl -sS --retry 30 --retry-delay 1 --retry-all-errors --max-time 3 \
  -o /dev/null -w "$origin/ answered %{http_code}\n" "$origin/"
test "$(docker inspect -f '{{.State.Running}}' "$container")" = true
