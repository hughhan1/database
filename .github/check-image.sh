#!/usr/bin/env bash
# Run IMAGE the way Holon runs this provider and check that it serves, at the
# transport URL in server.json, the server server.json names with the tools
# holon-contract.json declares. Needs docker, jq and holon.
set -euo pipefail

image=$1
package=$(jq -c '.packages[0]' server.json)
address=$(jq -r '.transport.url | sub("^[a-z]+://"; "")' <<<"$package")
port=${address%%/*}
port=${port##*:}
path=/${address#*/}

# Holon sets every variable server.json declares. The provider reads them on
# first use, so placeholders let it start without a database.
environment=()
for name in $(jq -r '.environmentVariables[].name' <<<"$package"); do
  environment+=(-e "$name=placeholder")
done

container=$(docker create -p "127.0.0.1::$port" \
  --read-only --tmpfs /tmp --user 10001:10001 --cap-drop ALL \
  "${environment[@]}" "$image")
trap 'docker logs "$container"; docker rm -f "$container" >/dev/null' EXIT
docker start "$container" >/dev/null
origin="http://$(docker port "$container" "$port/tcp")"
curl -sS --retry 30 --retry-delay 1 --retry-all-errors --max-time 3 -o /dev/null "$origin/"

served=$(holon list "$origin$path" --json)
diff -u --label "server.json and holon-contract.json" --label "served by $image" \
  <(jq -nS --slurpfile server server.json --slurpfile contract holon-contract.json \
    '{server: ($server[0] | {name, version}), tools: $contract[0].schema.tools}') \
  <(jq -S '{server: .serverInfo, tools: [.tools[] | {name, inputSchema, outputSchema}]}' <<<"$served")
echo "$image serves $(jq -r '.serverInfo | "\(.name) \(.version)"' <<<"$served") at $path"
