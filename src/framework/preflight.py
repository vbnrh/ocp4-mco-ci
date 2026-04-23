"""
Pre-flight sanity checks before deployment.

Validates configuration, file existence, credentials, and network
settings before any AWS resources are provisioned.
"""

import ipaddress
import json
import logging
import os
import shutil
import subprocess

from src.framework import config
from src.utility import constants

logger = logging.getLogger(__name__)


class PreflightError(Exception):
    """Raised when a critical pre-flight check fails."""

    pass


def _is_deploying_ocp():
    """Check if any cluster is actually deploying OCP (not skip_ocp_deployment)."""
    for cluster in config.clusters:
        if not cluster.ENV_DATA.get("skip_ocp_deployment"):
            return True
    return False


def run_preflight_checks():
    """
    Run all pre-flight sanity checks.
    Collects all failures and reports them together.

    When skip_ocp_deployment is set on all clusters (e.g. submariner-only
    re-runs), skip checks that only apply to cluster creation (SSH key,
    pull secret image access, CIDR overlap) and instead verify that the
    existing clusters are reachable.
    """
    errors = []
    warnings = []
    deploying_ocp = _is_deploying_ocp()

    _check_oc_binary(errors)
    _check_aws_credentials(errors, warnings)

    if deploying_ocp:
        _check_pull_secret(errors, warnings)
        _check_ssh_key(errors, warnings)
        if config.multicluster and config.nclusters > 1:
            _check_cidr_overlap(errors)
        _check_template_files(errors)
        _check_acm_config(errors, warnings)
    else:
        _check_cluster_reachability(errors, warnings)

    _check_cluster_config(errors)

    # Report
    for w in warnings:
        logger.warning(f"[PREFLIGHT WARNING] {w}")

    if errors:
        logger.error("=" * 60)
        logger.error("PRE-FLIGHT CHECK FAILED - Fix these before deploying:")
        logger.error("=" * 60)
        for i, e in enumerate(errors, 1):
            logger.error(f"  {i}. {e}")
        logger.error("=" * 60)
        raise PreflightError(
            f"{len(errors)} pre-flight check(s) failed. See log above."
        )

    logger.info(
        f"All pre-flight checks passed "
        f"({len(warnings)} warning(s))"
    )


def _check_oc_binary(errors):
    """Verify oc CLI is available."""
    if not shutil.which("oc"):
        errors.append(
            "'oc' binary not found on PATH. "
            "Install OpenShift CLI or add bin/ to PATH."
        )


def _check_pull_secret(errors, warnings):
    """Verify pull secret exists, has required registry auths, and can pull key images."""
    pull_secret_path = os.path.join(constants.TOP_DIR, "data", "pull-secret")
    if not os.path.exists(pull_secret_path):
        errors.append(
            f"Pull secret not found at {pull_secret_path}. "
            f"Copy data/pull-secret.template and fill in credentials."
        )
        return

    try:
        with open(pull_secret_path, "r") as f:
            ps = json.loads(f.read())
    except (json.JSONDecodeError, IOError) as e:
        errors.append(f"Pull secret is not valid JSON: {e}")
        return

    auths = ps.get("auths", {})
    required_registries = [
        "registry.redhat.io",
        "quay.io",
    ]

    # Nightly OCP installer pulls from registry.ci.openshift.org
    installer_version = config.DEPLOYMENT.get("installer_version", "")
    if "nightly" in installer_version:
        required_registries.append("registry.ci.openshift.org")

    # OCS registry image is on quay.io/rhceph-dev
    if not config.ENV_DATA.get("skip_ocs_deployment", True):
        required_registries.append("quay.io/rhceph-dev")

    # CNV requires additional registries
    if config.MULTICLUSTER.get("deploy_cnv"):
        required_registries.append("quay.io/openshift-cnv")

    # ACM unreleased requires brew + quay.io:443
    if config.MULTICLUSTER.get("acm_hub_unreleased"):
        required_registries.extend([
            "brew.registry.redhat.io",
            "quay.io:443",
        ])

    for reg in required_registries:
        if reg not in auths:
            errors.append(
                f"Pull secret missing auth for '{reg}'. "
                f"Add credentials to {pull_secret_path}"
            )

    # Verify actual image pull access for key images
    _verify_image_pull_access(pull_secret_path, errors, warnings)


def _resolve_nightly_version(version, errors):
    """
    Resolve a partial nightly version (e.g. 4.22.0-0.nightly) to the latest
    accepted full build tag via the OCP release API.

    Returns:
        str: Full nightly version string, or None on failure
    """
    import requests

    url = (
        f"https://amd64.ocp.releases.ci.openshift.org/api/v1/"
        f"releasestream/{version}/latest"
    )
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code != 200:
            errors.append(
                f"Cannot resolve OCP nightly version '{version}': "
                f"API returned {resp.status_code}. "
                f"Check if the release stream exists."
            )
            return None
        resolved = resp.json().get("name")
        logger.info(
            f"[PREFLIGHT] OCP nightly resolved: {version} -> {resolved}"
        )
        return resolved
    except requests.exceptions.ConnectionError:
        errors.append(
            f"Cannot reach OCP release API ({url}). "
            f"Check network connectivity."
        )
        return None
    except Exception as e:
        errors.append(f"Failed to resolve OCP nightly version: {e}")
        return None


def _verify_image_pull_access(pull_secret_path, errors, warnings):
    """
    Test that the pull secret can actually access key container images.
    Uses 'oc image info' to verify pull access without downloading full images.
    """
    images_to_check = []

    # OCP nightly: resolve partial tag (e.g. 4.22.0-0.nightly) to full build ID
    # This also validates registry.ci.openshift.org auth
    installer_version = config.DEPLOYMENT.get("installer_version", "")
    if "nightly" in installer_version:
        resolved = _resolve_nightly_version(installer_version, errors)
        if resolved:
            images_to_check.append(
                (
                    f"registry.ci.openshift.org/ocp/release:{resolved}",
                    "OCP nightly installer",
                )
            )

    # OCS registry (catalog source) image
    ocs_image = config.ENV_DATA.get("ocs_registry_image", "")
    if ocs_image and not config.ENV_DATA.get("skip_ocs_deployment", True):
        images_to_check.append((ocs_image, "ODF catalog source"))

    # CNV nightly catalog image
    if config.MULTICLUSTER.get("deploy_cnv"):
        cnv_version = config.ENV_DATA.get("cnv_version", "4.22")
        images_to_check.append(
            (
                f"quay.io/openshift-cnv/nightly-catalog:{cnv_version}",
                "CNV nightly catalog",
            )
        )

    if not images_to_check:
        return

    # Check if oc is available for image inspection
    if not shutil.which("oc"):
        warnings.append(
            "Cannot verify image pull access — 'oc' not on PATH"
        )
        return

    for image, description in images_to_check:
        try:
            result = subprocess.run(
                [
                    "oc", "image", "info",
                    "--filter-by-os", "linux/amd64",
                    "--registry-config", pull_secret_path,
                    image,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip()
                if "unauthorized" in stderr.lower() or "denied" in stderr.lower():
                    errors.append(
                        f"Pull secret cannot access {description} image "
                        f"({image}): unauthorized. Check registry credentials."
                    )
                elif "not found" in stderr.lower() or "manifest unknown" in stderr.lower():
                    errors.append(
                        f"{description} image not found: {image}. "
                        f"Verify the image tag exists."
                    )
                else:
                    warnings.append(
                        f"Could not verify {description} image ({image}): {stderr}"
                    )
            else:
                logger.info(f"[PREFLIGHT] Image accessible: {description} ({image})")
        except subprocess.TimeoutExpired:
            warnings.append(
                f"Timeout verifying {description} image ({image}) — "
                f"registry may be slow"
            )
        except Exception as e:
            warnings.append(
                f"Error checking {description} image ({image}): {e}"
            )


def _check_ssh_key(errors, warnings):
    """Verify SSH key exists (only needed when deploying OCP)."""
    ssh_key = os.path.expanduser(
        config.DEPLOYMENT.get("ssh_key", "~/.ssh/id_ed25519.pub")
    )
    if not os.path.isfile(ssh_key):
        errors.append(
            f"SSH public key not found at {ssh_key}. "
            f"Set DEPLOYMENT.ssh_key to a valid path."
        )


def _check_aws_credentials(errors, warnings):
    """Verify AWS credentials are configured."""
    # Check environment variables first, then credentials file
    has_env = os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get(
        "AWS_SECRET_ACCESS_KEY"
    )
    has_file = os.path.exists(os.path.expanduser("~/.aws/credentials"))
    has_profile = os.environ.get("AWS_PROFILE")

    if not (has_env or has_file or has_profile):
        errors.append(
            "No AWS credentials found. Set AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY "
            "environment variables, or configure ~/.aws/credentials"
        )
        return

    # Try a quick STS call to verify credentials are valid
    try:
        result = subprocess.run(
            ["aws", "sts", "get-caller-identity"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            warnings.append(
                f"AWS credentials may be invalid: {result.stderr.strip()}"
            )
    except FileNotFoundError:
        warnings.append(
            "'aws' CLI not found — cannot verify AWS credentials. "
            "Deployment uses boto3 directly, so this may still work."
        )
    except Exception:
        warnings.append("Could not verify AWS credentials (timeout or error)")


def _check_cidr_overlap(errors):
    """
    Verify cluster network CIDRs don't overlap.
    Overlapping CIDRs will cause Submariner to fail.
    """
    cluster_nets = []
    service_nets = []

    for cluster in config.clusters:
        name = cluster.ENV_DATA.get("cluster_name", f"cluster-{len(cluster_nets)}")
        cn = cluster.ENV_DATA.get("cluster_network_cidr")
        sn = cluster.ENV_DATA.get("service_network_cidr")

        if cn:
            try:
                cluster_nets.append((name, ipaddress.ip_network(cn)))
            except ValueError as e:
                errors.append(f"Invalid cluster_network_cidr '{cn}' for {name}: {e}")
        if sn:
            try:
                service_nets.append((name, ipaddress.ip_network(sn)))
            except ValueError as e:
                errors.append(f"Invalid service_network_cidr '{sn}' for {name}: {e}")

    # Check pairwise overlap
    for i in range(len(cluster_nets)):
        for j in range(i + 1, len(cluster_nets)):
            n1_name, n1 = cluster_nets[i]
            n2_name, n2 = cluster_nets[j]
            if n1.overlaps(n2):
                errors.append(
                    f"cluster_network_cidr overlap: {n1_name}={n1} and {n2_name}={n2}. "
                    f"Submariner requires non-overlapping CIDRs."
                )

    for i in range(len(service_nets)):
        for j in range(i + 1, len(service_nets)):
            n1_name, n1 = service_nets[i]
            n2_name, n2 = service_nets[j]
            if n1.overlaps(n2):
                errors.append(
                    f"service_network_cidr overlap: {n1_name}={n1} and {n2_name}={n2}. "
                    f"Submariner requires non-overlapping CIDRs."
                )


def _check_template_files(errors):
    """Verify critical template files exist on disk."""
    templates_to_check = []

    # Always needed for OCS
    if not config.ENV_DATA.get("skip_ocs_deployment", True):
        templates_to_check.extend([
            constants.SUBSCRIPTION_YAML,
            constants.NAMESPACE_TEMPLATE,
        ])

    # CNV templates
    if config.MULTICLUSTER.get("deploy_cnv"):
        templates_to_check.extend([
            constants.CNV_NS_YAML,
            constants.CNV_SUBSCRIPTION_YAML,
            constants.CNV_CATALOG_SOURCE_YAML,
            constants.CNV_HYPERCONVERGED_YAML,
        ])

    # ACM templates
    if config.MULTICLUSTER.get("deploy_acm_hub_cluster"):
        templates_to_check.extend([
            constants.ACM_HUB_OPERATORGROUP_YAML,
            constants.ACM_HUB_SUBSCRIPTION_YAML,
            constants.ACM_HUB_MULTICLUSTERHUB_YAML,
        ])

    # GitOps templates
    if not config.MULTICLUSTER.get("skip_gitops_deployment", True):
        templates_to_check.extend([
            constants.GITOPS_SUBSCRIPTION_YAML,
            constants.GITOPS_CLUSTER_YAML,
            constants.GITOPS_PLACEMENT_YAML,
            constants.GITOPS_PLACEMENT_RBAC_YAML,
        ])

    # DR templates
    if config.MULTICLUSTER.get("configure_discovered_dr"):
        templates_to_check.extend([
            constants.OADP_SUBSCRIPTION_YAML,
            constants.OADP_NS_YAML,
        ])

    for tmpl in templates_to_check:
        if not os.path.isfile(tmpl):
            errors.append(f"Template file missing: {tmpl}")


def _check_acm_config(errors, warnings):
    """Validate ACM-specific configuration."""
    if not config.MULTICLUSTER.get("deploy_acm_hub_cluster"):
        return

    if config.MULTICLUSTER.get("acm_hub_unreleased"):
        # Need unreleased image tags
        acm_img = config.MULTICLUSTER.get(
            "acm_unreleased_image",
            config.MULTICLUSTER.get("default_acm_unreleased_image", ""),
        )
        if not acm_img or "DOWNSTREAM" not in acm_img:
            errors.append(
                "acm_hub_unreleased=true but acm_unreleased_image is not set "
                "or doesn't look like a downstream tag."
            )

        mce_img = config.MULTICLUSTER.get("mce_unreleased_image", "")
        if not mce_img or "DOWNSTREAM" not in mce_img:
            errors.append(
                "acm_hub_unreleased=true but mce_unreleased_image is not set "
                "or doesn't look like a downstream tag."
            )

        # Auth.yaml needed for downstream registry credentials
        auth_path = os.path.join(constants.TOP_DIR, "data", constants.AUTHYAML)
        if not os.path.isfile(auth_path):
            warnings.append(
                f"ACM unreleased deployment may need {auth_path} for "
                f"downstream registry credentials."
            )


def _check_cluster_config(errors):
    """Validate basic cluster configuration."""
    for i, cluster in enumerate(config.clusters):
        name = cluster.ENV_DATA.get("cluster_name", "")
        if not name:
            errors.append(f"Cluster {i}: cluster_name is not set")

        if not cluster.ENV_DATA.get("cluster_path"):
            errors.append(f"Cluster {i} ({name}): cluster_path is not set")

        if not cluster.ENV_DATA.get("base_domain"):
            errors.append(f"Cluster {i} ({name}): base_domain is not set")

        region = cluster.ENV_DATA.get("region")
        if not region:
            errors.append(f"Cluster {i} ({name}): region is not set")


def _check_cluster_reachability(errors, warnings):
    """
    Verify existing clusters are reachable when skip_ocp_deployment is set.
    Used for day-2 operations like submariner re-runs where clusters
    already exist and we just need to confirm they're accessible.

    Also checks submariner status if configure_submariner is enabled.
    """
    for i, cluster in enumerate(config.clusters):
        name = cluster.ENV_DATA.get("cluster_name", f"cluster-{i}")
        cluster_path = cluster.ENV_DATA.get("cluster_path", "")
        kubeconfig_location = cluster.RUN.get(
            "kubeconfig_location", "auth/kubeconfig"
        )
        kubeconfig = os.path.join(cluster_path, kubeconfig_location)

        if not cluster_path:
            errors.append(
                f"Cluster {name}: cluster_path is not set — "
                f"cannot verify reachability"
            )
            continue

        if not os.path.isfile(kubeconfig):
            errors.append(
                f"Cluster {name}: kubeconfig not found at {kubeconfig}. "
                f"Cluster may not have been deployed or path is wrong."
            )
            continue

        # Test cluster access with oc whoami
        try:
            result = subprocess.run(
                ["oc", "whoami", "--kubeconfig", kubeconfig],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                errors.append(
                    f"Cluster {name}: not reachable "
                    f"(oc whoami failed: {result.stderr.strip()})"
                )
            else:
                logger.info(
                    f"[PREFLIGHT] Cluster {name} reachable as "
                    f"{result.stdout.strip()}"
                )
        except subprocess.TimeoutExpired:
            errors.append(
                f"Cluster {name}: timeout connecting to cluster. "
                f"API server may be down."
            )
        except Exception as e:
            errors.append(f"Cluster {name}: error checking reachability: {e}")

    # Check submariner status if this is a submariner run
    if config.MULTICLUSTER.get("configure_submariner"):
        _check_submariner_status(warnings)


def _check_submariner_status(warnings):
    """
    Check if submariner is already installed and healthy on the clusters.
    This is informational — submariner re-runs are expected to fix issues,
    so these are warnings, not errors.
    """
    for i, cluster in enumerate(config.clusters):
        name = cluster.ENV_DATA.get("cluster_name", f"cluster-{i}")
        cluster_path = cluster.ENV_DATA.get("cluster_path", "")
        kubeconfig_location = cluster.RUN.get(
            "kubeconfig_location", "auth/kubeconfig"
        )
        kubeconfig = os.path.join(cluster_path, kubeconfig_location)

        if not os.path.isfile(kubeconfig):
            continue

        try:
            result = subprocess.run(
                [
                    "oc", "get", "submariner", "-A",
                    "--kubeconfig", kubeconfig,
                    "-o", "jsonpath={.items[*].status.globalCIDR}",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                warnings.append(
                    f"Cluster {name}: submariner not found or not accessible"
                )
            else:
                global_cidr = result.stdout.strip()
                if global_cidr:
                    logger.info(
                        f"[PREFLIGHT] Cluster {name}: submariner present "
                        f"(globalCIDR: {global_cidr})"
                    )
                else:
                    warnings.append(
                        f"Cluster {name}: submariner CR exists but "
                        f"globalCIDR not set — may need re-configuration"
                    )
        except subprocess.TimeoutExpired:
            warnings.append(
                f"Cluster {name}: timeout checking submariner status"
            )
        except Exception as e:
            warnings.append(
                f"Cluster {name}: error checking submariner: {e}"
            )
