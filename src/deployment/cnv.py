import logging
import tempfile
import time

from src.framework import config
from src.utility import constants, templating
from src.utility.cmd import exec_cmd
from src.deployment.operator_deployment import OperatorDeployment
from src.ocs.ocp import OCP

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
        """
        logger.info("Creating CNV namespace and operator group")
        exec_cmd(f"oc apply -f {constants.CNV_NS_YAML}")
        logger.info("Deploying CNV operator")
        self.deploy_operator(
            subscription_yaml=constants.CNV_SUBSCRIPTION_YAML,
        )

    def create_hyperconverged(self):
        """
        Create HyperConverged CR to trigger CNV deployment.
        """
        logger.info("Creating HyperConverged resource")
        exec_cmd(f"oc apply -f {constants.CNV_HYPERCONVERGED_YAML}")
        logger.info("Waiting for HyperConverged to become Available")
        # Wait briefly for the CR status columns to populate, otherwise
        # get_resource() fails with ValueError when the AVAILABLE column
        # doesn't exist yet in the oc get output.
        time.sleep(30)
        hco = OCP(
            resource_name="kubevirt-hyperconverged",
            namespace=constants.CNV_NAMESPACE,
            kind="HyperConverged",
        )
        hco.wait_for_resource(
            condition="True",
            resource_name="kubevirt-hyperconverged",
            column="AVAILABLE",
            timeout=900,
            sleep=15,
        )
        logger.info("HyperConverged deployment succeeded")

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
