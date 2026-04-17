import logging
import os
import glob
import tempfile

from src.framework import config
from src.utility import constants
from src.utility.cmd import exec_cmd
from src.ocs.ocp import OCP

logger = logging.getLogger(__name__)

WORKLOADS_TEMPLATE_DIR = os.path.join(constants.TEMPLATE_DIR, "workloads")

# Workload definitions: name -> appset template subdirectory
WORKLOAD_APPSETS = {
    "busybox_rbd": "busybox-rbd",
    "busybox_cephfs": "busybox-cephfs",
    "busybox_mix": "busybox-mix",
    "mysql_rbd": "mysql-rbd",
    "mysql_cephfs": "mysql-cephfs",
    "cnv_vm_pvc": "cnv-vm-pvc",
    "cnv_vm_dv": "cnv-vm-dv",
    "cnv_vm_dvt": "cnv-vm-dvt",
}


class WorkloadDeployment:
    def __init__(self):
        self.managed_cluster_names = []
        self.cluster_set = ""

    def get_managed_cluster_info(self):
        """
        Get managed cluster names and clusterset from ACM hub.
        """
        managed_clusters = (
            OCP(kind=constants.ACM_MANAGEDCLUSTER).get().get("items", [])
        )
        for cluster in managed_clusters:
            name = cluster["metadata"]["name"]
            if name == constants.ACM_LOCAL_CLUSTER:
                continue
            self.managed_cluster_names.append(name)
            if not self.cluster_set:
                self.cluster_set = cluster["metadata"]["labels"].get(
                    constants.ACM_CLUSTERSET_LABEL, ""
                )
        logger.info(
            f"Managed clusters: {self.managed_cluster_names}, "
            f"ClusterSet: {self.cluster_set}"
        )

    def apply_appset(self, appset_yaml_path):
        """
        Read an appset YAML, substitute PLACEHOLDER values, and apply it.

        Args:
            appset_yaml_path (str): Path to the appset YAML file
        """
        with open(appset_yaml_path, "r") as f:
            content = f.read()

        # Substitute cluster names in Placement matchExpressions values
        cluster_values = "\n".join(
            [f"                - {name}" for name in self.managed_cluster_names]
        )
        content = content.replace(
            "                - PLACEHOLDER", cluster_values, 1
        )

        # Substitute clusterset in Placement clusterSets
        content = content.replace(
            "    - PLACEHOLDER", f"    - {self.cluster_set}", 1
        )

        # Write to temp file and apply
        temp = tempfile.NamedTemporaryFile(
            mode="w", prefix="appset_", suffix=".yaml", delete=False
        )
        temp.write(content)
        temp.close()

        appset_name = os.path.basename(appset_yaml_path)
        logger.info(f"Applying ApplicationSet: {appset_name}")
        exec_cmd(f"oc apply -f {temp.name}")

    def create_cnv_vm_prereqs(self, workload_name):
        """
        Create prerequisites for CNV VM workloads on managed clusters.
        CNV VMs need an SSH key secret in each workload namespace.
        """
        if not workload_name.startswith("cnv_vm"):
            return

        appset_subdir = WORKLOAD_APPSETS.get(workload_name)
        appset_dir = os.path.join(WORKLOADS_TEMPLATE_DIR, appset_subdir)
        appset_files = sorted(glob.glob(os.path.join(appset_dir, "*.yaml")))

        ssh_key = os.path.expanduser(
            config.DEPLOYMENT.get("ssh_key", "~/.ssh/id_ed25519.pub")
        )
        if not os.path.exists(ssh_key):
            logger.warning(f"SSH key not found at {ssh_key}, skipping VM secret creation")
            return

        # Extract namespace names from the appset YAMLs
        for appset_file in appset_files:
            with open(appset_file, "r") as f:
                content = f.read()
            for line in content.split("\n"):
                if "namespace:" in line and "openshift-gitops" not in line:
                    ns = line.split("namespace:")[-1].strip()
                    logger.info(f"Creating VM SSH key secret in namespace {ns}")
                    exec_cmd(f"oc create ns {ns}", ignore_error=True)
                    exec_cmd(
                        f"oc create secret generic vm-secret-1 -n {ns} "
                        f"--from-file=key={ssh_key}",
                        ignore_error=True,
                    )

    def deploy_workload_type(self, workload_name):
        """
        Deploy all appsets for a given workload type.

        Args:
            workload_name (str): Key from WORKLOAD_APPSETS
        """
        appset_subdir = WORKLOAD_APPSETS.get(workload_name)
        if not appset_subdir:
            logger.warning(f"Unknown workload type: {workload_name}")
            return

        appset_dir = os.path.join(WORKLOADS_TEMPLATE_DIR, appset_subdir)
        if not os.path.isdir(appset_dir):
            logger.warning(f"AppSet directory not found: {appset_dir}")
            return

        appset_files = sorted(glob.glob(os.path.join(appset_dir, "*.yaml")))
        if not appset_files:
            logger.warning(f"No YAML files found in {appset_dir}")
            return

        logger.info(
            f"Deploying workload '{workload_name}': "
            f"{len(appset_files)} ApplicationSets"
        )
        for appset_file in appset_files:
            self.apply_appset(appset_file)

    def verify_applications(self):
        """
        Verify ArgoCD Applications are synced and healthy.
        """
        logger.info("Verifying ArgoCD Applications")
        apps = OCP(
            kind="Application.argoproj.io",
            namespace="openshift-gitops",
        ).get().get("items", [])

        healthy = 0
        degraded = []
        for app in apps:
            name = app["metadata"]["name"]
            health = (
                app.get("status", {}).get("health", {}).get("status", "Unknown")
            )
            sync = (
                app.get("status", {}).get("sync", {}).get("status", "Unknown")
            )
            if health == "Healthy" and sync == "Synced":
                healthy += 1
            else:
                degraded.append(f"{name} (health={health}, sync={sync})")

        logger.info(f"Healthy applications: {healthy}/{len(apps)}")
        if degraded:
            logger.warning(f"Degraded applications: {degraded}")

    def deploy(self):
        """
        Deploy all configured workloads.
        """
        workload_types = config.MULTICLUSTER.get("workload_types", [])
        if not workload_types:
            logger.info("No workload types configured, deploying all")
            workload_types = list(WORKLOAD_APPSETS.keys())

        self.get_managed_cluster_info()

        if not self.managed_cluster_names:
            logger.error("No managed clusters found, cannot deploy workloads")
            return

        for workload_type in workload_types:
            self.create_cnv_vm_prereqs(workload_type)
            self.deploy_workload_type(workload_type)

        self.verify_applications()
