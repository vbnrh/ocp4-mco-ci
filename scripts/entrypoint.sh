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

# Configure env.dr.yaml from environment variables
ENV_FILE="/app/config/env.dr.yaml"
if [ -f "$ENV_FILE" ]; then
  DEPLOY_TIME="${SCHEDULE_DEPLOY:-$(date -u -d '+2 minutes' '+%H:%M' 2>/dev/null || date -u -v+2M '+%H:%M')}"
  sed -i "s|schedule_time_deploy: .*|schedule_time_deploy: '$DEPLOY_TIME'|" "$ENV_FILE"
  [ -n "$SCHEDULE_CLEANUP" ] && sed -i "s|schedule_time_cleanup: .*|schedule_time_cleanup: '$SCHEDULE_CLEANUP'|" "$ENV_FILE"
  [ -n "$SCHEDULE_SUB" ] && sed -i "s|schedule_time_deploy_sub: .*|schedule_time_deploy_sub: '$SCHEDULE_SUB'|" "$ENV_FILE"
  [ -n "$WEBHOOK_URL" ] && sed -i "s|webhook_url: .*|webhook_url: '$WEBHOOK_URL'|" "$ENV_FILE"
  sed -i "s|use_installer_cache: .*|use_installer_cache: false|" "$ENV_FILE"
  echo "env.dr.yaml configured: deploy=$DEPLOY_TIME"
fi

exec python3 scripts/deploy-dr-ocp.py "$@"
