# RDR (Regional Disaster Recovery) Deployment Guide

This guide covers the automated end-to-end deployment of an OCP cluster pair configured for Regional Disaster Recovery (RDR) using the `vm-ocp-ci` framework.

## Architecture Overview

A minimal RDR setup requires:
- **2 OCP clusters on AWS** (Hub + Managed)
- The Hub cluster doubles as both the ACM hub and one of the managed/DR clusters
- The managed cluster is imported into ACM and participates in DR

```
                    +-----------------------+
                    |   Hub Cluster (DR2)   |
                    |   - ACM 2.16          |
                    |   - MCE 2.11          |
                    |   - MCO (DR Hub)      |
                    |   - GitOps (ArgoCD)   |
                    |   - ODF 4.22          |
                    |   - CNV 4.22          |
                    |   - OADP              |
                    +----------+------------+
                               |
                     Submariner (cross-cluster network)
                               |
                    +----------+------------+
                    | Managed Cluster (DR1)  |
                    |   - ODF 4.22           |
                    |   - CNV 4.22           |
                    |   - GitOps (ArgoCD)    |
                    |   - OADP               |
                    |   - DR Cluster Operator|
                    +------------------------+
```

## Prerequisites

1. **AWS credentials** configured (`~/.aws/credentials`)
2. **Pull secret** at `data/pull-secret` with access to:
   - `registry.redhat.io`
   - `quay.io`
   - `quay.io/rhceph-dev` (ODF nightly builds)
   - `brew.registry.redhat.io` (ACM downstream builds)
   - `quay.io/openshift-cnv` (CNV nightly catalog)
   - `quay.io/openshift-virtualization/konflux-builds` (CNV images)
3. **SSH key** (default: `~/.ssh/id_ed25519.pub`)
4. **Auth config** at `data/auth.yaml` with quay.io credentials for ACM downstream

---

## Deployment Sequence

The framework executes 13 steps in order:

```
OCP --> OCS --> CNV --> ACM --> GitOps --> MCO --> Submariner --> Import --> SSL --> DR --> Workloads --> Notifications
```

Each step is controlled by a config flag and will be skipped if the flag is not set.

---

## Step 1: OCP Cluster Deployment

**Config flag:** `ENV_DATA.skip_ocp_deployment: false`

Deploys OpenShift clusters on AWS using the `openshift-install` binary.

### Network CIDR Requirements

**CRITICAL:** Managed clusters MUST have non-overlapping network CIDRs for Submariner to work.

| Cluster | `cluster_network_cidr` | `service_network_cidr` |
|---------|----------------------|----------------------|
| DR1 (Managed) | `10.132.0.0/14` | `172.31.0.0/16` |
| DR2 (Hub) | `10.128.0.0/14` | `172.30.0.0/16` |

These are set in the override config files (`samples/2_cluster_acm_setup/`).

### Instance Types

| Node Type | Instance | vCPU | RAM |
|-----------|----------|------|-----|
| Master | `m5.xlarge` | 4 | 16 GB |
| Worker | `m5.2xlarge` | 8 | 32 GB |
| Worker count | 4 per cluster | | |

### Gotchas

- If `installer_version` ends with `.nightly`, the framework auto-resolves to the latest accepted nightly build.
- `force_download_installer: true` on the managed cluster, `false` on the hub (reuses the binary downloaded for the managed cluster).
- Parallel OCP deployment (`parallel_ocp_deployment: true`) only works when an ACM cluster is defined. For sequential deployment, set to `false`.
- The installer binary is cached in `./bin/`. If switching OCP versions, set `force_download_installer: true`.

---

## Step 2: ODF (OCS) Deployment

**Config flag:** `ENV_DATA.skip_ocs_deployment: false`

Deploys OpenShift Data Foundation (ODF/OCS) storage operator on all clusters.

### Configuration

```yaml
ENV_DATA:
  ocs_registry_image: "quay.io/rhceph-dev/ocs-registry:latest-stable-4.22.0"
  ocs_csv_channel: "stable-4.22"
  ocs_version: '4.22'
  skip_ocs_cluster_creation: false   # Also creates the StorageCluster
  enable_ocs_plugin: true            # Enables the ODF console plugin
```

### Gotchas

- The `ocs_registry_image` is a CatalogSource image, not the operator image itself. It replaces `redhat-operators` as the source for ODF.
- If the hub is also a DR primary cluster (`primary_cluster: true`), ODF will be installed on the hub too.
- If `skip_ocs_cluster_creation: true`, only the operator is installed (no StorageCluster CR). Useful when you want to create storage manually.
- OCS deployment runs in parallel across clusters using `multiprocessing`.

---

## Step 3: CNV (OpenShift Virtualization) Deployment

**Config flag:** `MULTICLUSTER.deploy_cnv: true`

Deploys OpenShift Virtualization on ALL clusters (hub + managed).

### Configuration

```yaml
ENV_DATA:
  cnv_version: '4.22'
```

### What It Does

1. Creates a CNV nightly CatalogSource (`quay.io/openshift-cnv/nightly-catalog:<version>`)
2. Creates the `openshift-cnv` namespace and OperatorGroup
3. Deploys the `kubevirt-hyperconverged` operator via OLM Subscription
4. Creates the HyperConverged CR with `useEmulation: true`
5. Waits for HyperConverged to become Available (timeout: 900s)

### Gotchas

- **Pull secret must include `quay.io/openshift-cnv` auth.** Without it, the CatalogSource will go into `TRANSIENT_FAILURE`. After updating the cluster pull-secret, you must wait for all MachineConfigPool nodes to finish rolling out before the catalog pod can pull images.
- **`useEmulation: true` is critical on AWS.** Standard AWS instances (m5.2xlarge) don't have `/dev/kvm`. The HyperConverged CR includes a jsonpatch annotation that enables QEMU software emulation, allowing VMs to run without hardware KVM support. Without this, VMs will be `ErrorUnschedulable` with "Insufficient devices.kubevirt.io/kvm".
- On bare metal or vSphere clusters with KVM support, emulation is not needed but doesn't hurt.
- CNV catalog uses `nightly-<version>` channel (e.g., `nightly-4.22`), not `stable`.

---

## Step 4: ACM (Advanced Cluster Management) Deployment

**Config flag:** `MULTICLUSTER.deploy_acm_hub_cluster: true`

Deploys ACM on the hub cluster only.

### Released vs Unreleased

```yaml
MULTICLUSTER:
  # For released ACM:
  acm_hub_unreleased: false
  acm_hub_channel: 'release-2.16'

  # For unreleased (downstream) ACM:
  acm_hub_unreleased: true
  acm_unreleased_image: "2.16.1-DOWNSTREAM-2026-04-14-12-41-13"
  mce_unreleased_image: "2.11.1-DOWNSTREAM-2026-04-16-18-29-12"
```

### Unreleased Deployment Flow

1. Clones `stolostron/deploy` repo
2. Patches `start.sh` to fix downstream pod name check
3. Creates ImageContentSourcePolicy for brew registry
4. Writes pull-secret with quay.io:443 token
5. Writes `snapshot.ver` with the image tag
6. Runs `./start.sh --silent`
7. Waits for MultiClusterHub CR to reach `Running` status

### Gotchas

- **ACM and MCE are separate images.** ACM 2.16 uses MCE 2.11, not MCE 2.16. Always verify the correct MCE tag.
- The unreleased deployment uses `acm-dev-catalog` and `mce-dev-catalog` CatalogSource names (not `acm-custom-registry`).
- ACM downstream uses `quay.io:443/acm-d` registry, which requires port-specific auth in the pull-secret.
- The `start.sh` script expects `COMPOSITE_BUNDLE=true` for downstream builds.
- Finding the latest ACM/MCE tags: check `quay.io/acm-d/acm-dev-catalog` and `quay.io/acm-d/mce-dev-catalog` for available tags.

---

## Step 5: GitOps (ArgoCD) Deployment

**Config flag:** `MULTICLUSTER.skip_gitops_deployment: false`

Deploys OpenShift GitOps operator on the hub, sets up ArgoCD-ACM integration on managed clusters.

### What It Does

**On the hub (ACM cluster):**
1. Deploys `openshift-gitops-operator` subscription (channel: `latest`)
2. Creates GitOps Cluster resource
3. Creates Placement resource for all managed clusters
4. Creates ManagedClusterSetBinding in `openshift-gitops` namespace
5. Creates RBAC (Role + RoleBinding) granting the `applicationset-controller` service account permission to list `PlacementDecisions` and `PlacementRules`
6. Waits for GitopsCluster to reach `successful` phase

**On managed clusters:**
1. Deploys `openshift-gitops-operator` subscription
2. Creates GitOps cluster role binding

### Gotchas

- **The placement RBAC is essential.** Without the `openshift-gitops-applicationset-controller-placement` Role/RoleBinding, ApplicationSets using `clusterDecisionResource` generator will fail with "no clusterDecisionResources found". This was discovered during testing and is now automated.
- GitOps uses `latest` channel (matching QE). This currently resolves to 1.20.x.
- GitOps must be deployed BEFORE MCO and DR because DR requires ArgoCD ApplicationSets.

---

## Step 6: MCO (Multicluster Orchestrator) Deployment

**Config flag:** `MULTICLUSTER.skip_mco_deployment: false`

Deploys `odf-multicluster-orchestrator` on the hub cluster. This also installs the DR Hub Operator automatically.

### Configuration

```yaml
MULTICLUSTER:
  skip_mco_deployment: false
  enable_mco_plugin: true    # Enables odf-multicluster-console plugin
```

### Gotchas

- MCO installs from the ODF CatalogSource (same `ocs_registry_image`), not from `redhat-operators`.
- The `odf-multicluster-console` plugin must be enabled to access the DR UI in the OCP console.
- The DR Hub Operator (`odr-hub-operator`) is installed automatically as a dependency of MCO.

---

## Step 7: Submariner Configuration

**Config flag:** `MULTICLUSTER.configure_submariner: true`

Configures Submariner for cross-cluster networking between DR clusters.

### Current Implementation (Upstream)

```yaml
MULTICLUSTER:
  configure_submariner: true
  submariner_source: "upstream"   # Only upstream is supported currently
```

1. Downloads `subctl` binary from submariner.io
2. Deploys broker on the primary cluster
3. Creates AWS IAM policy for Submariner
4. Assigns policy to each cluster's machine-api user
5. Prepares AWS cloud (security groups, ports) for each cluster
6. Joins each cluster to the broker
7. Verifies connections between clusters

### QE Approach (ACM-Based)

QE uses ACM-based Submariner deployment:
1. Create a ManagedClusterSet with both DR clusters
2. Install Submariner add-ons via ACM console
3. Provide AWS credentials (AccessKey/SecretKey)
4. Wait for connection/agent status to go healthy

This results in Submariner subscription with `stable-0.22` channel from `submariner-catalogsource`.

### Gotchas

- **Submariner frequently fails on first attempt.** The deploy script (`deploy-dr-ocp.py`) has a separate cron job to re-run Submariner configuration because "most of the time submariner won't be deployed successfully on previous cron job."
- The `subctl` commands use `--kubeconfig` to target specific clusters.
- AWS IAM policy assignment can fail silently if the cluster's machine-api user hasn't been created yet.
- `subctl join` has retry logic (5 attempts, 60s delay) because it's timing-sensitive.
- On AWS, Submariner needs ports 4500/UDP and 4490/UDP opened between clusters. The `subctl cloud prepare aws` command handles this.

---

## Step 8: Import Managed Clusters

**Config flag:** `MULTICLUSTER.import_managed_clusters: true`

Imports non-ACM clusters into the ACM hub.

### Gotchas

- After importing, the framework sleeps for 90 seconds to allow the managed cluster agent to connect.
- If the hub is also a managed cluster (`primary_cluster: true`), `local-cluster` is automatically present and doesn't need importing.

---

## Step 9: SSL Certificate Exchange

**Config flag:** `MULTICLUSTER.exchange_ssl_certificate: true`

Extracts ingress certificates from all clusters and creates a combined `user-ca-bundle` ConfigMap on each cluster, then patches the proxy to trust it.

### What It Does

1. Extracts `default-ingress-cert` from `openshift-config-managed` on each cluster
2. Combines all certs into a single `user-ca-bundle` ConfigMap
3. Creates the ConfigMap in `openshift-config` on ALL clusters (hub + managed)
4. Patches `proxy/cluster` to trust the `user-ca-bundle`

### Gotchas

- Each cluster typically has 2-3 certificates in its ingress cert bundle.
- The YAML indentation of the combined cert file must be correct or the ConfigMap will be invalid.
- This step is required for cross-cluster S3 access (Ramen/DR needs to talk to ODF on both clusters).

---

## Step 10: Discovered DR Configuration

**Config flag:** `MULTICLUSTER.configure_discovered_dr: true`

Configures DR for discovered applications. This is the core DR setup.

### What It Does

1. **Deploys OADP operator** on all clusters (hub + managed)
   - Subscription channel: `stable` (auto-resolves to latest stable version)
   - Creates DataProtectionApplication CR with Velero + AWS/OpenShift/KubeVirt plugins
2. **Creates MirrorPeer** on the hub
   - Uses ODF 4.19+ template for newer versions, older template for < 4.19
   - Sets cluster names (uses `local-cluster` if hub is also a DR cluster)
   - Waits for `ExchangedSecret` phase (timeout: 300s)
3. **Creates DRPolicy** on the hub
   - Links the two DR clusters
   - Waits for `Succeeded` status (timeout: 180s)
4. **Adds CA certificate to Ramen ConfigMap**
   - Extracts `user-ca-bundle` from `openshift-config`
   - Base64-encodes the cert
   - Adds `caCertificates` to all `s3StoreProfiles` in `ramen-hub-operator-config`

### Gotchas

- The MirrorPeer template differs between ODF < 4.19 and >= 4.19. The framework auto-detects the version.
- The primary cluster must be listed first in the MirrorPeer `spec.items`.
- If the hub is the primary cluster (`acm_cluster: true` + `primary_cluster: true`), its name in MirrorPeer is `local-cluster`, not the actual cluster name.
- The Ramen ConfigMap update is tricky: the `ramen_manager_config.yaml` key contains YAML-as-a-string, which requires special serialization with `|` literal block style.
- OADP `stable` channel is used (matching QE), which auto-resolves to the latest version (currently 1.5.5 for OCP 4.21, expected 1.6.x for OCP 4.22).

---

## Step 11: Workload Deployment

**Config flag:** `MULTICLUSTER.deploy_workloads: true`

Deploys RDR test workloads via ArgoCD ApplicationSets on the hub cluster.

### Configuration

```yaml
MULTICLUSTER:
  deploy_workloads: true
  workload_types:
    - busybox_rbd
    - mysql_rbd
    - cnv_vm_pvc
```

If `workload_types` is empty, ALL workload types are deployed.

### Available Workload Types

| Type | AppSets | Storage | Description |
|------|---------|---------|-------------|
| `busybox_rbd` | 6 | RBD | Busybox pods with RBD PVCs |
| `busybox_cephfs` | 4 | CephFS | Busybox pods with CephFS PVCs |
| `busybox_mix` | 1 | Mixed | Mixed RBD + CephFS |
| `mysql_rbd` | 3 | RBD | MySQL + io-writer pods |
| `mysql_cephfs` | 3 | CephFS | MySQL + io-writer pods |
| `cnv_vm_pvc` | 3 | RBD | VMs with PVC volumes |
| `cnv_vm_dv` | 3 | RBD | VMs with DataVolumes |
| `cnv_vm_dvt` | 3 | RBD | VMs with DataVolumeTemplates |

### What It Does

1. Queries ACM for managed cluster names and clusterset
2. For CNV workloads: pre-creates SSH key secrets (`vm-secret-1`) in target namespaces
3. Reads appset YAML templates from `src/templates/workloads/`
4. Substitutes `PLACEHOLDER` values with actual cluster names and clusterset
5. Applies ApplicationSets on the hub cluster
6. Verifies ArgoCD Applications are synced and healthy

### Gotchas

- **Templates are local copies** from the `ocs-workloads` repo, not cloned at runtime. This avoids network dependency and lets us fix upstream bugs.
- **MySQL appset path bug (fixed):** The upstream `ocs-workloads` repo has a copy-paste bug where mysql appsets reference `rdr/busybox/rbd/workloads/app-mysql-*` instead of `rdr/mysql/rbd/workloads/app-mysql-*`. Our local copies have this fixed.
- **CNV VM prereqs:** VMs need a `vm-secret-1` (SSH public key) Secret in each VM namespace before the VM can start. The framework auto-creates these from the configured `ssh_key`.
- **CNV VMs on AWS:** Require `useEmulation: true` on the HyperConverged CR (set in Step 3). Without it, VMs fail with `ErrorUnschedulable` because `/dev/kvm` doesn't exist on standard instances.
- **MySQL io-writer image:** The upstream `quay.io/prsurve/mysql_data_write:latest` image has a broken `/usr/bin/python3`. MySQL pods themselves work; the io-writer sidecar pods fail. This is an upstream issue.
- The placement RBAC from Step 5 must be in place or all ApplicationSets will fail with "no clusterDecisionResources found".
- ApplicationSets use ACM Placement with `clusterDecisionResource` generator (not the simpler `list` generator). This requires the `acm-placement` ConfigMap in `openshift-gitops`.

---

## Step 12-13: Notifications

**Config flags:**
- `REPORTING.email.skip_notification: true/false`
- `REPORTING.messenger.skip_notification: true/false`

Sends deployment status via email or Slack/Google Chat webhook.

---

## Running a Deployment

### Command

```bash
deploy-ocp multicluster 2 \
  --cluster1 --cluster-name dr1-apr-17-26 --cluster-path /tmp/dr1-apr-17-26 \
    --ocp4mcoci-conf samples/2_cluster_acm_setup/override_config.yaml \
  --cluster2 --cluster-name dr2-apr-17-26 --cluster-path /tmp/dr2-apr-17-26 \
    --ocp4mcoci-conf samples/2_cluster_acm_setup/override_hub_config.yaml \
  --webhook-url 'https://chat.googleapis.com/...'
```

### Scheduled Deployment

Use `scripts/deploy-dr-ocp.py` with `config/env.dr.yaml`:

```yaml
ocp_config: "samples/2_cluster_acm_setup/override_config.yaml"
ocp_hub_config: "samples/2_cluster_acm_setup/override_hub_config.yaml"
schedule_time_deploy: "06:00"
schedule_time_deploy_sub: "11:00"
webhook_url: "https://chat.googleapis.com/..."
```

The scheduler runs deployments weekdays at the configured time. A separate Submariner cron runs later because Submariner frequently fails on the first attempt.

---

## Current Version Matrix (rdr-4.22 branch)

| Component | Version | Source | Channel |
|-----------|---------|--------|---------|
| OCP | 4.22.0-0.nightly | CI | - |
| ODF/OCS | 4.22 | `quay.io/rhceph-dev/ocs-registry:latest-stable-4.22.0` | `stable-4.22` |
| ACM | 2.16.1 | `quay.io/acm-d/acm-dev-catalog` (downstream) | `release-2.16` |
| MCE | 2.11.1 | `quay.io/acm-d/mce-dev-catalog` (downstream) | `stable-2.11` |
| GitOps | latest (1.20.x) | `redhat-operators` | `latest` |
| OADP | latest stable (1.5.x) | `redhat-operators` | `stable` |
| CNV | 4.22 nightly | `quay.io/openshift-cnv/nightly-catalog:4.22` | `nightly-4.22` |
| Submariner | upstream | `get.submariner.io` | - |

---

## Troubleshooting

### CNV VMs stuck in ErrorUnschedulable

**Symptom:** VMs show `ErrorUnschedulable` with "Insufficient devices.kubevirt.io/kvm"

**Cause:** AWS instances don't have `/dev/kvm` hardware support.

**Fix:** Ensure HyperConverged CR has the emulation annotation:
```yaml
metadata:
  annotations:
    kubevirt.kubevirt.io/jsonpatch: '[{ "op": "add", "path": "/spec/configuration/developerConfiguration", "value": { "useEmulation": true } }]'
```
After patching, delete existing virt-handler pods (`oc delete pods -n openshift-cnv -l kubevirt.io=virt-handler`) and then delete stuck VMIs to force rescheduling.

### CNV CatalogSource in TRANSIENT_FAILURE

**Symptom:** `cnv-nightly-catalog-source` CatalogSource shows `TRANSIENT_FAILURE`

**Cause:** Cluster pull-secret doesn't include `quay.io/openshift-cnv` auth.

**Fix:**
1. Update the cluster pull-secret: `oc set data secret/pull-secret -n openshift-config --from-file=.dockerconfigjson=<pull-secret-file>`
2. Wait for MachineConfigPool rollout to complete on ALL nodes
3. Delete the failing catalog source pod to force recreation

### ApplicationSets show "no clusterDecisionResources found"

**Symptom:** All ApplicationSets in `ErrorOccurred` state.

**Cause:** The `applicationset-controller` service account lacks RBAC to list PlacementDecisions.

**Fix:** Apply the placement RBAC:
```bash
oc apply -f src/templates/gitops-deployment/placement_rbac.yaml
```

### Submariner fails to connect

**Symptom:** `subctl show connections` shows no connections.

**Common causes:**
1. AWS security groups not opened (run `subctl cloud prepare aws` again)
2. IAM policy not attached to the cluster's machine-api user
3. Overlapping CIDRs between clusters (must be different)
4. Gateway node not labeled

**Fix:** Re-run the Submariner configuration step. The framework's deploy script has a separate cron for this reason.

### MirrorPeer stuck in ExchangingSecret

**Symptom:** MirrorPeer never reaches `ExchangedSecret` phase.

**Common causes:**
1. Submariner not configured (cross-cluster connectivity required)
2. SSL certificates not exchanged
3. ODF not running on both clusters

### ACM downstream deploy fails

**Symptom:** `start.sh` errors out.

**Common causes:**
1. Wrong quay.io:443 token in pull-secret
2. MCE tag doesn't exist in `quay.io/acm-d/mce-dev-catalog`
3. ImageContentSourcePolicy not applied

**Debug:** Check `oc get pods -n open-cluster-management` and `oc get catalogsource -n openshift-marketplace`.
