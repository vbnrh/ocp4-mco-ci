import logging
import tempfile

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
        """
        if config.multicluster:
            for cluster in config.clusters:
                index = cluster.MULTICLUSTER["multicluster_index"]
                config.switch_ctx(index)
                logger.info(
                    f"Deploying CNV for cluster {cluster.ENV_DATA['cluster_name']}"
                )
                self.create_cnv_catalog_source()
                self.deploy_cnv_operator()
                self.create_hyperconverged()
            config.switch_default_cluster_ctx()
