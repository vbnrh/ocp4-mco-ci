import logging
import tempfile
import time

from src.framework import config
from src.utility import constants, templating
from src.utility.cmd import exec_cmd
from src.deployment.operator_deployment import OperatorDeployment

logger = logging.getLogger(__name__)


class CNVDeployment(OperatorDeployment):
    def __init__(self):
        super().__init__(constants.CNV_NAMESPACE, constants.CNV_OPERATOR_NAME)

    def create_cnv_catalog_source(self):
        """
        Create CNV nightly catalog source.
        Updates the catalog image tag based on the configured CNV version.
        """
        cnv_version = config.ENV_DATA.get("cnv_version", "4.22")
        catalog_data = templating.load_yaml(constants.CNV_CATALOG_SOURCE_YAML)
        catalog_data["spec"]["image"] = (
            f"quay.io/openshift-cnv/nightly-catalog:{cnv_version}"
        )
        catalog_manifest = tempfile.NamedTemporaryFile(
            mode="w+", prefix="cnv_catalog_source_", delete=False
        )
        templating.dump_data_to_temp_yaml(catalog_data, catalog_manifest.name)
        exec_cmd(f"oc apply -f {catalog_manifest.name}")

    def deploy_cnv_operator(self):
        """
        Deploy CNV operator via OLM subscription.

        The nightly catalog source may have different channels than the
        default redhat-operators catalog (e.g. 'nightly-4.21' instead of
        'stable'). We query the packagemanifest filtered to the nightly
        catalog to pick the correct channel, avoiding a mismatch where
        deploy_operator() resolves a CSV from redhat-operators that
        doesn't exist in the nightly catalog.
        """
        logger.info("Creating CNV namespace and operator group")
        exec_cmd(f"oc apply -f {constants.CNV_NS_YAML}")

        # Find the right channel from the nightly catalog's packagemanifest
        cnv_channel = self._get_cnv_nightly_channel()
        logger.info(f"Deploying CNV operator (channel: {cnv_channel})")
        self.deploy_operator(
            subscription_yaml=constants.CNV_SUBSCRIPTION_YAML,
            channel=cnv_channel,
            operator_selector=f"catalog={constants.CNV_CATALOG_SOURCE_NAME}",
        )

    def _get_cnv_nightly_channel(self):
        """
        Query the nightly catalog's packagemanifest to find the best
        channel for the configured CNV version.

        Prefers 'nightly-X.Y' channel matching cnv_version, falls back
        to the catalog's default channel.
        """
        cnv_version = config.ENV_DATA.get("cnv_version", "4.21")
        target_channel = f"nightly-{cnv_version}"

        try:
            result = exec_cmd(
                f"oc get packagemanifest kubevirt-hyperconverged "
                f"-l catalog={constants.CNV_CATALOG_SOURCE_NAME} "
                f"-n {constants.MARKETPLACE_NAMESPACE} "
                f"-o jsonpath='{{.items[0].status.channels[*].name}}'"
            )
            available_channels = result.stdout.decode().strip("'").split()
            logger.info(
                f"CNV nightly catalog channels: {available_channels}"
            )
            if target_channel in available_channels:
                return target_channel
            logger.warning(
                f"Channel {target_channel} not found in nightly catalog, "
                f"available: {available_channels}. Using default channel."
            )
            # Fall back to default channel from the nightly catalog
            result = exec_cmd(
                f"oc get packagemanifest kubevirt-hyperconverged "
                f"-l catalog={constants.CNV_CATALOG_SOURCE_NAME} "
                f"-n {constants.MARKETPLACE_NAMESPACE} "
                f"-o jsonpath='{{.items[0].status.defaultChannel}}'"
            )
            return result.stdout.decode().strip("'")
        except Exception as e:
            logger.warning(
                f"Failed to query nightly catalog channels: {e}. "
                f"Falling back to {target_channel}"
            )
            return target_channel

    def create_hyperconverged(self):
        """
        Create HyperConverged CR to trigger CNV deployment.
        """
        logger.info("Creating HyperConverged resource")
        exec_cmd(f"oc apply -f {constants.CNV_HYPERCONVERGED_YAML}")
        logger.info("Waiting for HyperConverged to become Available")
        # HyperConverged in 4.21+ doesn't expose AVAILABLE as a table column,
        # so check the Available condition via jsonpath instead.
        for _ in range(60):
            try:
                result = exec_cmd(
                    "oc get hyperconverged kubevirt-hyperconverged"
                    f" -n {constants.CNV_NAMESPACE}"
                    " -o jsonpath='{.status.conditions[?(@.type==\"Available\")].status}'"
                )
                status = result.stdout.decode().strip().strip("'")
                if status == "True":
                    logger.info("HyperConverged deployment succeeded")
                    return
                logger.debug(f"HyperConverged Available={status}, waiting...")
            except Exception:
                logger.debug("HyperConverged status not ready yet, waiting...")
            time.sleep(15)
        raise Exception("HyperConverged did not become Available within timeout")

    def do_deploy_cnv(self):
        """
        Deploy CNV on all clusters (hub + managed).
        Each cluster is handled independently so that a failure on one
        cluster does not skip CNV deployment on the remaining clusters.
        """
        failed_clusters = []
        if config.multicluster:
            for cluster in config.clusters:
                index = cluster.MULTICLUSTER["multicluster_index"]
                cluster_name = cluster.ENV_DATA["cluster_name"]
                config.switch_ctx(index)
                try:
                    logger.info(f"Deploying CNV for cluster {cluster_name}")
                    self.create_cnv_catalog_source()
                    self.deploy_cnv_operator()
                    self.create_hyperconverged()
                except Exception:
                    logger.error(
                        f"CNV deployment failed on cluster {cluster_name}",
                        exc_info=True,
                    )
                    failed_clusters.append(cluster_name)
            config.switch_default_cluster_ctx()
        if failed_clusters:
            raise Exception(
                f"CNV deployment failed on cluster(s): {', '.join(failed_clusters)}"
            )
