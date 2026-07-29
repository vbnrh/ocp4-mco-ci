import logging
import os
import sys
import time
import multiprocessing as mp
import yaml

from src.deployment.ocp import OCPDeployment
from src.deployment.ocs import OCSDeployment
from src.deployment.mco import MCODeployment
from src.deployment.acm import ACMDeployment
from src.deployment.gitops import GitopsDeployment
from src.deployment.ssl_certificate import SSLCertificate
from src.deployment.submariner import Submariner, run_subctl_cmd
from src.deployment.import_managed_cluster import ImportManagedCluster
from src import framework
from src.utility import constants
from src.utility.constants import LOG_FORMAT
from src.utility.cmd import exec_cmd
from src.utility.utils import (
    is_cluster_running,
    get_non_acm_cluster_config,
    get_kube_config_path,
)
from src.utility.email import email_reports
from src.utility.messenger import message_reports
from src.deployment.discovered_dr import DiscoveredDR
from src.deployment.cnv import CNVDeployment
from src.deployment.workloads import WorkloadDeployment
from src.ocs.ocp import OCP

log = logging.getLogger(__name__)


def set_log_level(log_cli_level):
    """
    Set the log level of this module based on the pytest.ini log_cli_level
    Args:
        config (pytest.config): Pytest config object
    """
    level = log_cli_level or "INFO"
    log.setLevel(logging.getLevelName(level))

    level = log_cli_level or "INFO"
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.getLevelName(level))

    # Prevent adding multiple handlers during repeated runs
    if not root_logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root_logger.addHandler(handler)


class Deployment(object):
    def __init__(self):
        pass

    def _cmd_output(self, cmd):
        """Run a command silently; return decoded stdout or empty string."""
        result = exec_cmd(cmd, ignore_error=True, silent=True)
        if result.returncode != 0:
            return ""
        return result.stdout.decode().strip().strip("'")

    def detect_completed_phases(self):
        """
        Per-cluster, per-component detection of what's already deployed.
        Uses only simple oc commands (no pipes/shell operators — exec_cmd
        uses shlex.split without shell=True).
        """
        per_cluster = {"ocp": {}, "ocs": {}, "cnv": {}, "oadp": {}}
        hub_only = {
            "acm": False, "gitops": False, "mco": False,
            "submariner": False, "import": False, "ssl": False,
            "mirror_peer": False, "dr_policy": False, "ramen_config": False,
        }

        try:
            for i in range(framework.config.nclusters):
                framework.config.switch_ctx(i)
                name = framework.config.ENV_DATA["cluster_name"]
                kubeconfig = get_kube_config_path(
                    framework.config.ENV_DATA["cluster_path"]
                )

                per_cluster["ocp"][i] = (
                    os.path.exists(kubeconfig)
                    and self._cmd_output(f"oc --kubeconfig {kubeconfig} cluster-info") != ""
                )
                if not per_cluster["ocp"][i]:
                    per_cluster["ocs"][i] = False
                    per_cluster["cnv"][i] = False
                    per_cluster["oadp"][i] = False
                    continue

                per_cluster["ocs"][i] = (
                    self._cmd_output(
                        "oc get storagecluster -n openshift-storage"
                        " -o jsonpath={.items[0].status.phase}"
                    ) == "Ready"
                )

                per_cluster["cnv"][i] = (
                    "True" in self._cmd_output(
                        "oc get hyperconverged -n openshift-cnv"
                        ' -o jsonpath={.items[0].status.conditions[?(@.type=="Available")].status}'
                    )
                )

                per_cluster["oadp"][i] = (
                    "Succeeded" in self._cmd_output(
                        "oc get csv -n openshift-adp"
                        " -o jsonpath={.items[0].status.phase}"
                    )
                )

                is_hub = (
                    framework.config.multicluster
                    and framework.config.get_acm_index() == i
                )
                if is_hub:
                    hub_only["acm"] = (
                        self._cmd_output(
                            "oc get multiclusterhub -A"
                            " -o jsonpath={.items[0].status.phase}"
                        ) == "Running"
                    )
                    hub_only["gitops"] = (
                        "Succeeded" in self._cmd_output(
                            "oc get csv -n openshift-gitops"
                            " -o jsonpath={.items[0].status.phase}"
                        )
                    )
                    hub_only["mco"] = (
                        "Succeeded" in self._cmd_output(
                            "oc get csv -n openshift-operators"
                            " -o jsonpath={.items[?(@.spec.displayName==\"DF Multicluster Orchestrator\")].status.phase}"
                        )
                    )

            if framework.config.multicluster:
                # Submariner — check gateway pod on each cluster
                gw_count = 0
                for cluster in framework.config.clusters:
                    idx = cluster.MULTICLUSTER["multicluster_index"]
                    framework.config.switch_ctx(idx)
                    gw = self._cmd_output(
                        "oc get pods -n submariner-operator"
                        " -l app=submariner-gateway"
                        " -o jsonpath={.items[0].status.phase}"
                    )
                    if gw == "Running":
                        gw_count += 1
                hub_only["submariner"] = gw_count == framework.config.nclusters

                framework.config.switch_acm_ctx()

                # Import — count non-local ManagedClusters
                mc_names = self._cmd_output(
                    "oc get managedclusters"
                    " -o jsonpath={.items[*].metadata.name}"
                )
                if mc_names:
                    non_local = [n for n in mc_names.split() if n != "local-cluster"]
                    hub_only["import"] = len(non_local) > 0

                # SSL — check on all clusters
                ssl_ok = True
                for i in range(framework.config.nclusters):
                    framework.config.switch_ctx(i)
                    if not self._cmd_output(
                        "oc get cm user-ca-bundle -n openshift-config"
                        " -o jsonpath={.metadata.name}"
                    ):
                        ssl_ok = False
                        break
                hub_only["ssl"] = ssl_ok

                framework.config.switch_acm_ctx()

                phase = self._cmd_output(
                    "oc get mirrorpeer"
                    " -o jsonpath={.items[0].status.phase}"
                )
                hub_only["mirror_peer"] = phase in ("Ready", "ExchangedSecret")

                hub_only["dr_policy"] = (
                    "Succeeded" in self._cmd_output(
                        "oc get drpolicy"
                        " -o jsonpath={.items[0].status.conditions[0].reason}"
                    )
                )

                hub_only["ramen_config"] = (
                    self._cmd_output(
                        f"oc get cm {constants.DR_RAMEN_HUB_OPERATOR_CONFIG}"
                        f" -n {constants.OPENSHIFT_OPERATORS}"
                        " -o jsonpath={.metadata.name}"
                    ) != ""
                )

        except Exception as ex:
            log.warning(f"Error during phase detection (non-fatal): {ex}")
        finally:
            framework.config.switch_default_cluster_ctx()

        # Log summary table
        log.info("=" * 60)
        log.info("DEPLOYMENT STATE DETECTION")
        log.info("=" * 60)
        for comp, cluster_map in per_cluster.items():
            for idx, done in cluster_map.items():
                framework.config.switch_ctx(idx)
                name = framework.config.ENV_DATA["cluster_name"]
                status = "DONE" if done else "INCOMPLETE"
                log.info(f"  {comp:20s} | {name:25s} | {status}")
        for comp, done in hub_only.items():
            status = "DONE" if done else "INCOMPLETE"
            log.info(f"  {comp:20s} | {'hub':25s} | {status}")
        log.info("=" * 60)
        framework.config.switch_default_cluster_ctx()

        # Aggregate into phase-level skip decisions
        completed = set()

        def _all_clusters_done(comp):
            return all(per_cluster[comp].get(i, False)
                       for i in range(framework.config.nclusters))

        if _all_clusters_done("ocp"):
            completed.add("deploy_ocp")
        if _all_clusters_done("ocs"):
            completed.add("deploy_ocs")
        if _all_clusters_done("cnv"):
            completed.add("deploy_cnv")
        if hub_only["acm"]:
            completed.add("deploy_acm")
        if hub_only["gitops"]:
            completed.add("deploy_gitops")
        if hub_only["mco"]:
            completed.add("deploy_mco")
        if hub_only["submariner"]:
            completed.add("configure_submariner")
        if hub_only["import"]:
            completed.add("aws_import_cluster")
        if hub_only["ssl"]:
            completed.add("ssl_certificate")
        if (
            _all_clusters_done("oadp")
            and hub_only["mirror_peer"]
            and hub_only["dr_policy"]
            and hub_only["ramen_config"]
        ):
            completed.add("configure_discovered_dr")

        return completed

    def deploy_ocp(self, log_cli_level="INFO"):
        # OCP Deployment
        processes = []
        parallel = False
        for i in range(framework.config.nclusters):
            framework.config.switch_ctx(i)
            if framework.config.MULTICLUSTER["acm_cluster"]:
                # Enable parallel deployment only if ACM cluster is present and the flag is true
                parallel = framework.config.MULTICLUSTER.get(
                    "parallel_ocp_deployment", False
                )
        for i in range(framework.config.nclusters):
            try:
                framework.config.switch_ctx(i)
                cluster_path = framework.config.ENV_DATA["cluster_path"]
                cluster_name = framework.config.ENV_DATA["cluster_name"]
                if not framework.config.ENV_DATA.get("skip_ocp_deployment", True):
                    if is_cluster_running(cluster_path):
                        log.warning(
                            "OCP cluster is already running, skipping installation"
                        )
                    else:
                        log.info(f"Deploying OCP cluster for {cluster_name}")
                        ocp_deployment = OCPDeployment(cluster_name, cluster_path)
                        ocp_deployment.deploy_prereq()
                        if parallel:
                            # Prepare for parallel deployment
                            p = mp.Process(
                                target=OCPDeployment.deploy_ocp,
                                args=(
                                    ocp_deployment.installer_binary_path,
                                    ocp_deployment.cluster_path,
                                    log_cli_level,
                                ),
                            )
                            processes.append(p)
                        else:
                            # Sequential deployment
                            OCPDeployment.deploy_ocp(
                                ocp_deployment.installer_binary_path,
                                ocp_deployment.cluster_path,
                                log_cli_level,
                            )
                else:
                    log.warning("OCP deployment will be skipped")
            except Exception as ex:
                log.error("Unable to deploy OCP cluster !", exc_info=True)
        framework.config.switch_default_cluster_ctx()
        if parallel and len(processes) > 0:
            for proc in processes:
                proc.start()
            # complete the processes
            for proc in processes:
                proc.join()

    def deploy_ocs(self, log_cli_level):
        # OCS Deployment
        processes = []
        for i in range(framework.config.nclusters):
            try:
                framework.config.switch_ctx(i)
                if not framework.config.ENV_DATA["skip_ocs_deployment"]:
                    if (
                        framework.config.multicluster
                        and framework.config.get_acm_index() == i
                        and not framework.config.MULTICLUSTER["primary_cluster"]
                    ):
                        continue
                    log.info("Deploying OCS Operator")
                    ocs_deployment = OCSDeployment()
                    ocs_deployment.deploy_prereq()
                    p = mp.Process(
                        target=OCSDeployment.deploy_ocs,
                        args=(
                            get_kube_config_path(
                                framework.config.ENV_DATA["cluster_path"]
                            ),
                            framework.config.ENV_DATA["skip_ocs_cluster_creation"],
                        ),
                    )
                    processes.append(p)
                else:
                    log.warning("OCS deployment will be skipped")
            except Exception as ex:
                log.error("Unable to deploy OCS cluster", exc_info=True)
        framework.config.switch_default_cluster_ctx()
        if len(processes) > 0:
            log.info(f"Creating OCS cluster on {len(processes)} clusters")
            [proc.start() for proc in processes]
            # complete the processes
            for proc in processes:
                proc.join()

    def deploy_mco(self):
        if not getattr(self, '_gitops_deployed', False):
            log.warning(
                "Skipping MCO deployment — GitOps is not deployed "
                "(MCO's odf-multicluster-orchestrator requires ArgoCD APIs)"
            )
            return
        # MCO Deployment
        for i in range(framework.config.nclusters):
            try:
                framework.config.switch_ctx(i)
                if (
                    framework.config.multicluster
                    and framework.config.get_acm_index() == i
                ):
                    if not framework.config.MULTICLUSTER["skip_mco_deployment"]:
                        log.info("Deploying MCO Operator")
                        mco_deployment = MCODeployment()
                        mco_deployment.deploy_prereq()
                        MCODeployment.deploy_mco()
                    else:
                        log.warning("MCO deployment will be skipped")
            except Exception as ex:
                log.error("Unable to deploy MCO operator", exc_info=True)
        framework.config.switch_default_cluster_ctx()
        self._patch_ramen_hub_namespace()

    def _patch_ramen_hub_namespace(self):
        """
        Workaround for 4.22 ramen bug: drClusterOperator defaults are wrong:
        - namespaceName is empty → defaults to openshift-operators → dual OG
        - catalogSourceName is 'ramen-catalog' → doesn't exist on managed clusters
        - channelName is 'alpha' → wrong for released builds
        Patch the configmap with correct values.
        """
        framework.config.switch_acm_ctx()
        try:
            ramen_cm = OCP(
                kind="ConfigMap",
                resource_name=constants.DR_RAMEN_HUB_OPERATOR_CONFIG,
                namespace=constants.OPENSHIFT_OPERATORS,
            )
            ramen_cm.get()
            ramen_config = yaml.safe_load(
                ramen_cm.data["data"][constants.DR_RAMEN_CONFIG_MANAGER_KEY]
            )
            dr_op = ramen_config.setdefault("drClusterOperator", {})
            needs_patch = False
            if dr_op.get("namespaceName") != "openshift-dr-system":
                dr_op["namespaceName"] = "openshift-dr-system"
                needs_patch = True
            if dr_op.get("catalogSourceName") != "redhat-operators":
                dr_op["catalogSourceName"] = "redhat-operators"
                needs_patch = True
            if dr_op.get("channelName") != "stable-4.22":
                dr_op["channelName"] = "stable-4.22"
                needs_patch = True
            if dr_op.get("packageName") != "odr-cluster-operator":
                dr_op["packageName"] = "odr-cluster-operator"
                needs_patch = True
            if dr_op.get("catalogSourceNamespaceName") != "openshift-marketplace":
                dr_op["catalogSourceNamespaceName"] = "openshift-marketplace"
                needs_patch = True
            channel = dr_op.get("channelName", "stable-4.22")
            pkg_json = self._cmd_output(
                "oc get packagemanifest -n openshift-marketplace odr-cluster-operator -o json"
            )
            csv_name = ""
            if pkg_json:
                import json as _json
                try:
                    channels = _json.loads(pkg_json).get("status", {}).get("channels", [])
                    csv_name = next(
                        (ch["currentCSV"] for ch in channels if ch["name"] == channel), ""
                    )
                except (ValueError, KeyError):
                    pass
            if csv_name and dr_op.get("clusterServiceVersionName") != csv_name:
                dr_op["clusterServiceVersionName"] = csv_name
                needs_patch = True
            if not needs_patch:
                log.info("Ramen hub config already patched correctly")
                return
            log.info("Patching ramen-hub-operator-config with correct drClusterOperator values")
            ramen_cm.data["data"][constants.DR_RAMEN_CONFIG_MANAGER_KEY] = yaml.dump(
                ramen_config, default_flow_style=False
            )
            cm_data = dict(ramen_cm.data)
            for key in ["annotations", "creationTimestamp", "resourceVersion", "uid"]:
                cm_data.get("metadata", {}).pop(key, None)
            import tempfile
            cm_yaml = tempfile.NamedTemporaryFile(mode="w+", prefix="ramen_cm_patch_", delete=False)
            yaml.dump(cm_data, cm_yaml, default_flow_style=False)
            cm_yaml.flush()
            exec_cmd(f"oc apply -f {cm_yaml.name}")
            log.info("Ramen hub configmap patched successfully")
        except Exception as ex:
            log.warning(f"Failed to patch ramen hub configmap: {ex}")
        finally:
            framework.config.switch_default_cluster_ctx()

    def deploy_acm(self):
        # ACM Deployment
        for i in range(framework.config.nclusters):
            try:
                framework.config.switch_ctx(i)
                if (
                    framework.config.multicluster
                    and framework.config.get_acm_index() == i
                ):
                    if framework.config.MULTICLUSTER["deploy_acm_hub_cluster"]:
                        log.info("Deploying ACM")
                        acm_deployment = ACMDeployment()
                        if framework.config.MULTICLUSTER.get("acm_hub_unreleased"):
                            acm_deployment.deploy_acm_hub_unreleased()
                        else:
                            acm_deployment.deploy_acm_hub_released()
                    else:
                        log.warning("ACM deployment will be skipped")
            except Exception as ex:
                log.error("Unable to deploy ACM hub operator", exc_info=True)
        framework.config.switch_default_cluster_ctx()

    def configure_submariner(self):
        try:
            for i in range(framework.config.nclusters):
                framework.config.switch_ctx(i)
                if (
                    framework.config.multicluster
                    and framework.config.get_acm_index() == i
                ):
                    if framework.config.MULTICLUSTER["configure_submariner"]:
                        log.info("Configuring submariner")
                        submariner = Submariner()
                        submariner.deploy()
                    else:
                        log.warning("Submariner configuration will be skipped")
        except Exception as ex:
            log.error("Unable to configure submariner", exc_info=True)
        framework.config.switch_default_cluster_ctx()

    def aws_import_cluster(self):
        try:
            for i in range(framework.config.nclusters):
                framework.config.switch_ctx(i)
                if (
                    framework.config.multicluster
                    and framework.config.get_acm_index() == i
                ):
                    if framework.config.MULTICLUSTER["import_managed_clusters"]:
                        for cluster in get_non_acm_cluster_config():
                            log.info(
                                f"Importing cluster {cluster.ENV_DATA['cluster_name']} into ACM"
                            )
                            import_managed_cluster = ImportManagedCluster(
                                cluster.ENV_DATA["cluster_name"],
                                cluster.ENV_DATA["cluster_path"],
                            )
                            import_managed_cluster.import_cluster()
                        log.info(
                            "Sleeping for 90 seconds after importing managed cluster"
                        )
                        time.sleep(90)
                    else:
                        log.warning(f"Skipping managed cluster import")
        except Exception as ex:
            log.error("Unable to import cluster", exc_info=True)
        framework.config.switch_default_cluster_ctx()

    def deploy_gitops(self):
        self._gitops_deployed = False
        for i in range(framework.config.nclusters):
            try:
                framework.config.switch_ctx(i)
                if (
                    framework.config.multicluster
                    and framework.config.get_acm_index() == i
                ):
                    if not framework.config.MULTICLUSTER["skip_gitops_deployment"]:
                        log.info("Deploying GitOps Operator")
                        # Deploy operator subscription only on hub
                        framework.config.switch_ctx(framework.config.get_acm_index())
                        gitops_deployment = GitopsDeployment()
                        gitops_deployment.deploy_prereq()
                        GitopsDeployment.deploy_gitops()
                        # Role binding on managed clusters only
                        for cluster in framework.config.clusters:
                            index = cluster.MULTICLUSTER["multicluster_index"]
                            if framework.config.get_acm_index() != index:
                                framework.config.switch_ctx(index)
                                gitops_deployment = GitopsDeployment()
                                gitops_deployment.gitops_role_binding()
                        self._gitops_deployed = True
                    else:
                        log.warning("GitOps deployment will be skipped")
            except Exception as ex:
                log.error("Unable to deploy GitOps operator", exc_info=True)
        framework.config.switch_default_cluster_ctx()

    def ssl_certificate(self):
        try:
            for i in range(framework.config.nclusters):
                framework.config.switch_ctx(i)
                if (
                    framework.config.multicluster
                    and framework.config.get_acm_index() == i
                ):
                    if framework.config.MULTICLUSTER["exchange_ssl_certificate"]:
                        ssl_certificate = SSLCertificate()
                        for cluster in framework.config.clusters:
                            framework.config.switch_ctx(
                                cluster.MULTICLUSTER["multicluster_index"]
                            )
                            log.info("Fetching ssl secrets")
                            ssl_certificate.get_certificate()
                        ssl_certificate.get_certificate_file_path()
                        log.warning(ssl_certificate.ssl_certificate_path)
                        for cluster in framework.config.clusters:
                            framework.config.switch_ctx(
                                cluster.MULTICLUSTER["multicluster_index"]
                            )
                            log.info("Exchanging ssl secrets")
                            ssl_certificate.exchange_certificate()
                    else:
                        log.warning(
                            f"Skipping SSL certificate exchange for managed clusters"
                        )
        except Exception as ex:
            log.error(
                "Unable to configure SSL certificate for the cluster", exc_info=True
            )
        framework.config.switch_default_cluster_ctx()

    def send_email(self):
        # send email notification
        for i in range(framework.config.nclusters):
            framework.config.switch_ctx(i)
            skip_notification = framework.config.REPORTING["email"]["skip_notification"]
            if not skip_notification:
                email_reports()
            else:
                log.warning("Email notification will be skipped")
        framework.config.switch_default_cluster_ctx()

    def send_message(self):
        # send gchat message
        for i in range(framework.config.nclusters):
            framework.config.switch_ctx(i)
            skip_notification = framework.config.REPORTING["messenger"][
                "skip_notification"
            ]
            if not skip_notification:
                message_reports()
            else:
                log.warning("Gchat notification will be skipped")
        framework.config.switch_default_cluster_ctx()

    def deploy_cnv(self):
        # CNV Deployment — check from hub config since flag lives there
        if not framework.config.multicluster:
            log.warning("CNV deployment will be skipped (standalone cluster)")
            return
        try:
            framework.config.switch_acm_ctx()
            if framework.config.MULTICLUSTER.get("deploy_cnv"):
                log.info("Deploying CNV (OpenShift Virtualization)")
                cnv_deployment = CNVDeployment()
                cnv_deployment.do_deploy_cnv()
            else:
                log.warning("CNV deployment will be skipped")
        except Exception as ex:
            log.error("Unable to deploy CNV", exc_info=True)
        framework.config.switch_default_cluster_ctx()

    def deploy_workloads(self):
        # Deploy RDR test workloads via ArgoCD ApplicationSets
        if not framework.config.multicluster:
            log.warning("Workload deployment will be skipped (standalone cluster)")
            return
        try:
            framework.config.switch_acm_ctx()
            if framework.config.MULTICLUSTER.get("deploy_workloads"):
                log.info("Deploying RDR test workloads")
                workloads = WorkloadDeployment()
                workloads.deploy()
            else:
                log.warning("Workload deployment will be skipped")
        except Exception as ex:
            log.error("Unable to deploy workloads", exc_info=True)
        framework.config.switch_default_cluster_ctx()

    def configure_discovered_dr(self):
        if not framework.config.multicluster:
            log.warning("Discovered DR configuration will be skipped (standalone cluster)")
            return
        try:
            framework.config.switch_acm_ctx()
            if framework.config.MULTICLUSTER["configure_discovered_dr"]:
                log.info("Configuring DR setup for discovered applications")
                discovered_dr = DiscoveredDR()
                discovered_dr.deploy()
            else:
                log.warning("Discovered DR configuration will be skipped")
        except Exception:
            log.error("Unable to configure discovered DR", exc_info=True)
        framework.config.switch_default_cluster_ctx()

    def run_post_deploy_validation(self):
        """
        Run post-deployment validation checks with up to 3 retries.
        Logs results — does not block notifications on failure.
        """
        max_retries = 3
        retry_delay = 120

        try:
            for attempt in range(1, max_retries + 1):
                log.info(
                    "=" * 60 + f"\n"
                    f"POST-DEPLOYMENT VALIDATION (attempt {attempt}/{max_retries})\n"
                    + "=" * 60
                )
                results = self._collect_validation_results()

                failed = [r for r in results if r["status"] == "FAIL"]
                passed = [r for r in results if r["status"] == "PASS"]

                for r in passed:
                    log.info(
                        f"  [PASS] {r['component']}: {r['detail']}"
                    )
                for r in failed:
                    log.warning(
                        f"  [FAIL] {r['component']}: {r['detail']}"
                    )

                if not failed:
                    log.info(
                        f"All {len(passed)} validation checks passed"
                    )
                    break

                if attempt < max_retries:
                    log.warning(
                        f"{len(failed)} check(s) failed, "
                        f"retrying in {retry_delay}s..."
                    )
                    time.sleep(retry_delay)
                else:
                    log.error(
                        "=" * 60 + "\n"
                        f"VALIDATION FAILED — {len(failed)} check(s) "
                        f"still failing after {max_retries} attempts:\n"
                        + "=" * 60
                    )
                    for r in failed:
                        log.error(
                            f"  {r['component']}: {r['detail']}"
                        )
        except Exception:
            log.error(
                "Post-deployment validation encountered an error",
                exc_info=True,
            )

    def _collect_validation_results(self):
        """
        Collect validation results from all deployed components.
        Returns list of {"component", "status", "detail"} dicts.
        """
        results = []

        for i in range(framework.config.nclusters):
            framework.config.switch_ctx(i)
            name = framework.config.ENV_DATA["cluster_name"]

            # Nodes — always check
            results.append(self._check_nodes(name))

            # StorageCluster
            if not framework.config.ENV_DATA.get(
                "skip_ocs_deployment", True
            ):
                results.append(self._check_storage_cluster(name))

            # CNV HyperConverged
            if framework.config.MULTICLUSTER.get("deploy_cnv"):
                results.append(self._check_cnv(name))

            # ACM MultiClusterHub (hub only)
            if (
                framework.config.MULTICLUSTER.get("deploy_acm_hub_cluster")
                and framework.config.get_acm_index() == i
            ):
                results.append(self._check_acm(name))

        # Cross-cluster checks from hub context
        if framework.config.multicluster:
            framework.config.switch_acm_ctx()

            if framework.config.MULTICLUSTER.get("configure_submariner"):
                results.append(self._check_submariner())

            if framework.config.MULTICLUSTER.get("configure_discovered_dr"):
                results.extend(self._check_dr())

            if framework.config.MULTICLUSTER.get("deploy_workloads"):
                results.append(self._check_workloads())

        framework.config.switch_default_cluster_ctx()
        return results

    def _check_nodes(self, cluster_name):
        try:
            nodes = OCP(kind="node")
            node_data = nodes.get().get("items", [])
            not_ready = []
            for node in node_data:
                node_name = node["metadata"]["name"]
                conditions = node.get("status", {}).get("conditions", [])
                ready = any(
                    c["type"] == "Ready" and c["status"] == "True"
                    for c in conditions
                )
                if not ready:
                    not_ready.append(node_name)
            if not_ready:
                return {
                    "component": f"Nodes ({cluster_name})",
                    "status": "FAIL",
                    "detail": f"{len(not_ready)} not ready: "
                    + ", ".join(not_ready),
                }
            return {
                "component": f"Nodes ({cluster_name})",
                "status": "PASS",
                "detail": f"{len(node_data)} nodes ready",
            }
        except Exception as e:
            return {
                "component": f"Nodes ({cluster_name})",
                "status": "FAIL",
                "detail": str(e),
            }

    def _check_storage_cluster(self, cluster_name):
        try:
            sc = OCP(
                resource_name=constants.STORAGE_CLUSTER_NAME,
                namespace=constants.OPENSHIFT_STORAGE_NAMESPACE,
                kind="StorageCluster",
            )
            phase = sc.get_resource(
                constants.STORAGE_CLUSTER_NAME, "PHASE"
            )
            if phase == "Ready":
                return {
                    "component": f"StorageCluster ({cluster_name})",
                    "status": "PASS",
                    "detail": "Ready",
                }
            return {
                "component": f"StorageCluster ({cluster_name})",
                "status": "FAIL",
                "detail": f"Phase={phase}",
            }
        except Exception as e:
            return {
                "component": f"StorageCluster ({cluster_name})",
                "status": "FAIL",
                "detail": str(e),
            }

    def _check_cnv(self, cluster_name):
        try:
            result = exec_cmd(
                "oc get hyperconverged kubevirt-hyperconverged"
                f" -n {constants.CNV_NAMESPACE}"
                " -o jsonpath='{.status.conditions[?(@.type==\"Available\")].status}'"
            )
            available = result.stdout.decode().strip().strip("'")
            if available == "True":
                return {
                    "component": f"CNV HyperConverged ({cluster_name})",
                    "status": "PASS",
                    "detail": "Available=True",
                }
            return {
                "component": f"CNV HyperConverged ({cluster_name})",
                "status": "FAIL",
                "detail": f"Available={available}",
            }
        except Exception as e:
            return {
                "component": f"CNV HyperConverged ({cluster_name})",
                "status": "FAIL",
                "detail": str(e),
            }

    def _check_acm(self, cluster_name):
        try:
            mch = OCP(
                kind=constants.ACM_MULTICLUSTER_HUB,
                namespace=constants.ACM_HUB_NAMESPACE,
            )
            status = mch.get_resource(
                constants.ACM_MULTICLUSTER_RESOURCE, "STATUS"
            )
            if status == constants.STATUS_RUNNING:
                return {
                    "component": f"ACM MultiClusterHub ({cluster_name})",
                    "status": "PASS",
                    "detail": "Running",
                }
            return {
                "component": f"ACM MultiClusterHub ({cluster_name})",
                "status": "FAIL",
                "detail": f"Status={status}",
            }
        except Exception as e:
            return {
                "component": f"ACM MultiClusterHub ({cluster_name})",
                "status": "FAIL",
                "detail": str(e),
            }

    def _check_submariner(self):
        try:
            for i in range(framework.config.nclusters):
                cluster = framework.config.clusters[i]
                kube_config_path = get_kube_config_path(
                    cluster.ENV_DATA["cluster_path"]
                )
                connect_check = (
                    f"show connections --kubeconfig {kube_config_path}"
                )
                run_subctl_cmd(connect_check)
            return {
                "component": "Submariner",
                "status": "PASS",
                "detail": "Connections verified",
            }
        except Exception as e:
            return {
                "component": "Submariner",
                "status": "FAIL",
                "detail": str(e),
            }

    def _check_dr(self):
        results = []
        try:
            # MirrorPeer
            mp_ocp = OCP(
                kind="MirrorPeer",
                namespace=constants.OPENSHIFT_STORAGE_NAMESPACE,
            )
            mp_data = mp_ocp.get().get("items", [])
            if mp_data:
                phase = (
                    mp_data[0]
                    .get("status", {})
                    .get("phase", "Unknown")
                )
                if phase in ("ExchangedSecret", "Ready"):
                    results.append({
                        "component": "MirrorPeer",
                        "status": "PASS",
                        "detail": phase,
                    })
                else:
                    results.append({
                        "component": "MirrorPeer",
                        "status": "FAIL",
                        "detail": f"Phase={phase}",
                    })
            else:
                results.append({
                    "component": "MirrorPeer",
                    "status": "FAIL",
                    "detail": "No MirrorPeer resource found",
                })
        except Exception as e:
            results.append({
                "component": "MirrorPeer",
                "status": "FAIL",
                "detail": str(e),
            })

        try:
            # DRPolicy
            drp = OCP(kind="DRPolicy")
            drp_data = drp.get().get("items", [])
            if drp_data:
                conditions = (
                    drp_data[0]
                    .get("status", {})
                    .get("conditions", [])
                )
                reason = (
                    conditions[0].get("reason", "Unknown")
                    if conditions
                    else "NoConditions"
                )
                if reason == "Succeeded":
                    results.append({
                        "component": "DRPolicy",
                        "status": "PASS",
                        "detail": "Succeeded",
                    })
                else:
                    results.append({
                        "component": "DRPolicy",
                        "status": "FAIL",
                        "detail": f"Reason={reason}",
                    })
            else:
                results.append({
                    "component": "DRPolicy",
                    "status": "FAIL",
                    "detail": "No DRPolicy resource found",
                })
        except Exception as e:
            results.append({
                "component": "DRPolicy",
                "status": "FAIL",
                "detail": str(e),
            })

        return results

    def _check_workloads(self):
        try:
            apps = (
                OCP(
                    kind="Application.argoproj.io",
                    namespace="openshift-gitops",
                )
                .get()
                .get("items", [])
            )
            if not apps:
                return {
                    "component": "ArgoCD Applications",
                    "status": "FAIL",
                    "detail": "No applications found",
                }
            degraded = []
            for app in apps:
                name = app["metadata"]["name"]
                health = (
                    app.get("status", {})
                    .get("health", {})
                    .get("status", "Unknown")
                )
                sync = (
                    app.get("status", {})
                    .get("sync", {})
                    .get("status", "Unknown")
                )
                if health != "Healthy" or sync != "Synced":
                    degraded.append(
                        f"{name}(health={health},sync={sync})"
                    )
            if degraded:
                return {
                    "component": "ArgoCD Applications",
                    "status": "FAIL",
                    "detail": f"{len(degraded)}/{len(apps)} degraded: "
                    + ", ".join(degraded),
                }
            return {
                "component": "ArgoCD Applications",
                "status": "PASS",
                "detail": f"{len(apps)}/{len(apps)} healthy",
            }
        except Exception as e:
            return {
                "component": "ArgoCD Applications",
                "status": "FAIL",
                "detail": str(e),
            }
