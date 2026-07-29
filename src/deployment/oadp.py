import logging
import tempfile
import yaml

from src.framework import config
from src.utility import constants
from src.utility.cmd import exec_cmd
from src.deployment.operator_deployment import OperatorDeployment
from src.utility.utils import get_non_acm_cluster_config
from src.ocs.resources.catalog_source import CatalogSource


logger = logging.getLogger(__name__)

OADP_DEFAULT_CATALOG_IMAGE = "registry.redhat.io/redhat/redhat-operator-index:v4.21"
OADP_CATALOG_NAME = "oadp-catalog"


class OADPDeployment(OperatorDeployment):
    def __init__(self):
        super().__init__(constants.OADP_NAMESPACE, constants.OADP_OPERATOR_NAME)

    def _get_oadp_catalog_image(self):
        return (
            config.MULTICLUSTER.get("oadp_catalog_image")
            or OADP_DEFAULT_CATALOG_IMAGE
        )

    def _ensure_oadp_catalog_source(self):
        """
        Create a dedicated CatalogSource for OADP when it's not
        available in the cluster's default catalogs.
        The catalog image is configurable via MULTICLUSTER.oadp_catalog_image.
        """
        try:
            cs = CatalogSource(
                resource_name=OADP_CATALOG_NAME,
                namespace=constants.MARKETPLACE_NAMESPACE,
            )
            cs.get()
            logger.info(f"CatalogSource {OADP_CATALOG_NAME} already exists")
            return
        except Exception:
            pass

        catalog_image = self._get_oadp_catalog_image()
        logger.info(f"Creating CatalogSource {OADP_CATALOG_NAME} with image {catalog_image}")
        cs_data = {
            "apiVersion": "operators.coreos.com/v1alpha1",
            "kind": "CatalogSource",
            "metadata": {
                "name": OADP_CATALOG_NAME,
                "namespace": constants.MARKETPLACE_NAMESPACE,
            },
            "spec": {
                "displayName": "OADP Operator Catalog",
                "image": catalog_image,
                "publisher": "Red Hat",
                "sourceType": "grpc",
                "updateStrategy": {"registryPoll": {"interval": "10m"}},
            },
        }
        cs_yaml = tempfile.NamedTemporaryFile(
            mode="w+", prefix="oadp_catalog_", delete=False
        )
        yaml.dump(cs_data, cs_yaml, default_flow_style=False)
        cs_yaml.flush()
        exec_cmd(f"oc apply -f {cs_yaml.name}")
        cs = CatalogSource(
            resource_name=OADP_CATALOG_NAME,
            namespace=constants.MARKETPLACE_NAMESPACE,
        )
        cs.wait_for_state("READY")

    def do_deploy_oadp(self):
        """
        Deploy OADP Operator

        """
        if config.multicluster:
            managed_clusters = get_non_acm_cluster_config(include_acm=True)
            for cluster in managed_clusters:
                index = cluster.MULTICLUSTER["multicluster_index"]
                config.switch_ctx(index)
                logger.info(
                    f"Deploying OADP Operator for  cluster {cluster.ENV_DATA['cluster_name']}"
                )
                self._ensure_oadp_catalog_source()
                self.deploy_operator(
                    subscription_yaml=constants.OADP_SUBSCRIPTION_YAML,
                    ns_yaml=constants.OADP_NS_YAML,
                )
                logger.info("Creating Resource DataProtectionApplication")
                exec_cmd(f"oc apply -f {constants.DPA_DISCOVERED_APPS_PATH}")
            config.switch_default_cluster_ctx()
