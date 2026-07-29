#!/bin/bash
set -e

DEFAULT_VERSION="4.22.0-0.nightly-2026-07-16-135205"
REQUESTED="${OCP_VERSION:-$DEFAULT_VERSION}"

if [ "$REQUESTED" = "latest" ]; then
  echo "Resolving latest nightly..."
  REQUESTED=$(curl -sf "https://amd64.ocp.releases.ci.openshift.org/api/v1/releasestream/4.22.0-0.nightly/latest" \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['name'])")
  echo "Resolved: $REQUESTED"
fi

if [ ! -x /app/bin/openshift-install ] || ! /app/bin/openshift-install version 2>/dev/null | grep -q "$REQUESTED"; then
  echo "Downloading openshift-install $REQUESTED..."
  oc adm release extract \
    --registry-config /app/data/pull-secret \
    --command=openshift-install \
    --to /app/bin/ \
    "registry.ci.openshift.org/ocp/release:$REQUESTED"
  chmod +x /app/bin/openshift-install
  echo "Downloaded: $(/app/bin/openshift-install version | head -1)"
else
  echo "Using existing installer: $REQUESTED"
fi

export OCP_INSTALLER_VERSION="$REQUESTED"
export PATH="/app/bin:$PATH"

cd /app
exec python3 scripts/deploy-dr-ocp.py "$@"
