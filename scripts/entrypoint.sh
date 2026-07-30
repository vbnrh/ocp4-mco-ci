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

# Generate env.dr.yaml from environment variables
ENV_FILE="/app/config/env.dr.yaml"
DEPLOY_TIME="${SCHEDULE_DEPLOY:-$(date -u -d '+2 minutes' '+%H:%M' 2>/dev/null || date -u '+%H:%M')}"
CLEANUP_TIME="${SCHEDULE_CLEANUP:-20:00}"
SUB_TIME="${SCHEDULE_SUB:-09:30}"
HOOK="${WEBHOOK_URL:-}"

cat > "$ENV_FILE" <<YAML
ocp_config: './samples/2_cluster_acm_setup/override_config.yaml'
ocp_hub_config: './samples/2_cluster_acm_setup/override_hub_config.yaml'
ocp_sub_config: './samples/configure_submariner/override_config.yaml'
ocp_hub_sub_config: './samples/configure_submariner/override_hub_config.yaml'
use_installer_cache: false
cache_version: '4.22.0-0.nightly-2026-07-16-135205'
schedule_time_cleanup: '$CLEANUP_TIME'
schedule_time_deploy: '$DEPLOY_TIME'
schedule_time_deploy_sub: '$SUB_TIME'
webhook_url: '$HOOK'
YAML
echo "env.dr.yaml generated: deploy=$DEPLOY_TIME cleanup=$CLEANUP_TIME sub=$SUB_TIME"

exec python3 scripts/deploy-dr-ocp.py "$@"
