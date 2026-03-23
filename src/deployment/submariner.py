import logging

import requests
import tempfile
import os
import boto3
from src.utility.retry import retry
from botocore.exceptions import ClientError
from src.framework import config
from src.utility.cmd import exec_cmd
from src.utility.nodes import get_typed_worker_nodes
from src.utility.exceptions import CommandFailed, DRPrimaryNotFoundException
from src.utility import constants
from src.utility.utils import (
    get_non_acm_cluster_config,
    get_kube_config_path,
    delete_file_with_prefix,
    get_cluster_metadata,
)

logger = logging.getLogger(__name__)
iam = boto3.client("iam")


def get_api_username(cluster_name):
    username_matcher = f"{cluster_name}-"
    username = ""
    try:
        paginator = iam.get_paginator("list_users")
        for page in paginator.paginate():
            for user in page["Users"]:
                if (
                    username_matcher in user["UserName"]
                    and "openshift-machine-api-aws" in user["UserName"]
                ):
                    username = user["UserName"]
                    break
        return username
    except ClientError as error:
        logger.error(f"Unable to find aws api {username_matcher}")
        raise error


def get_infra_id(cluster_path):
    get_cluster_metadata(cluster_path)["infraID"]


def get_aws_user_id():
    try:
        sts = boto3.client("sts")
        response = sts.get_caller_identity()
        return response["Account"]
    except ClientError as error:
        logger.error("Unable to find aws user id")
        raise error


def assign_aws_policy(cluster_name):
    try:
        print("Assigning a policy to aws API user")
        policy = (
            "arn:aws:iam::"
            + get_aws_user_id()
            + ":policy/"
            + constants.AWS_IAM_POLICY_NAME
        )
        username = get_api_username(cluster_name)
        iam.attach_user_policy(UserName=username, PolicyArn=policy)
    except ClientError as error:
        logger.error("Unable to assign aws policy")
        raise error


def remove_aws_policy(cluster_name):
    try:
        print("Removing a policy to aws API user")
        policy = (
            "arn:aws:iam::"
            + get_aws_user_id()
            + ":policy/"
            + constants.AWS_IAM_POLICY_NAME
        )
        username = get_api_username(cluster_name)
        if username != "":
            iam.detach_user_policy(UserName=username, PolicyArn=policy)
    except ClientError as error:
        logger.error("Unable to remove aws policy")


def create_aws_policy():
    policy = open(os.path.join(constants.AWS_IAM_POLICY_JSON), "r")
    try:
        iam.create_policy(
            PolicyName=constants.AWS_IAM_POLICY_NAME, PolicyDocument=policy.read()
        )
    except iam.exceptions.EntityAlreadyExistsException as ex:
        logger.warning(f"AWS policy {constants.AWS_IAM_POLICY_NAME} already exists")


def create_aws_policy():
    policy = open(os.path.join(constants.AWS_IAM_POLICY_JSON), "r")
    try:
        iam.create_policy(
            PolicyName=constants.AWS_IAM_POLICY_NAME, PolicyDocument=policy.read()
        )
    except iam.exceptions.EntityAlreadyExistsException as ex:
        logger.warning(f"AWS policy {constants.AWS_IAM_POLICY_NAME} already exists")


def run_subctl_cmd(cmd=None):
    """
    Run subctl command
    Args:
        cmd: subctl command to be executed
    """
    cmd = " ".join(["subctl", cmd])
    exec_cmd(cmd)


class Submariner(object):
    """
    Submariner configuaration and deployment
    """

    def __init__(self):
        # whether upstream OR downstream
        self.source = config.MULTICLUSTER["submariner_source"]
        # Designated broker cluster index where broker will be deployed
        self.designated_broker_cluster_index = self.get_primary_cluster_index()
        # sequence number for the clusters from submariner perspective
        # Used mainly to run submariner commands, for each cluster(except ACM hub) we will
        # assign a seq number with 1 as primary and continue with subsequent numbers
        self.cluster_seq = 1
        # List of index to all the clusters which are participating in DR (except ACM)
        # i.e index in the config.clusters list
        self.dr_only_list = []

    def deploy(self):
        if self.source == "upstream":
            self.deploy_upstream()
        else:
            raise Exception(f"The Submariner source: {self.source} is not recognized")

    def deploy_upstream(self):
        self.download_binary()
        self.submariner_configure_upstream()

    def download_binary(self):
        if self.source == "upstream":
            # This script puts the platform specific binary in ~/.local/bin
            # we need to move the subctl binary to ocs-ci/bin dir
            downloader_prefix = "submariner_downloader_"
            try:
                submarier_url = (
                    config.MULTICLUSTER["submariner_url"]
                    or constants.SUBMARINER_DOWNLOAD_URL
                )
                resp = requests.get(submarier_url)
            except requests.ConnectionError:
                logger.error(
                    "Failed to download the downloader script from submariner site"
                )
                raise
            delete_file_with_prefix(downloader_prefix)
            tempf = tempfile.NamedTemporaryFile(
                dir=".", mode="wb", prefix=downloader_prefix, delete=False
            )
            tempf.write(resp.content)

            # Actual submariner binary download
            cmd = f"bash {tempf.name}"
            try:
                exec_cmd(cmd)
            except CommandFailed:
                logger.error("Failed to download submariner binary")
                raise

            # Copy submariner from ~/.local/bin to ocs-ci/bin
            # ~/.local/bin is the default path selected by submariner script
            dest_path = os.path.join(config.RUN["bin_dir"], "subctl")
            if not os.path.exists(dest_path):
                os.symlink(
                    os.path.expanduser("~/.local/bin/subctl"),
                    dest_path,
                )

    @retry(CommandFailed, tries=5, delay=60, backoff=1)
    def join_cluster(self, cluster):
        # Join all the clusters (except ACM cluster in case of hub deployment)
        cluster_index = cluster.MULTICLUSTER["multicluster_index"]
        if (
            cluster_index != config.get_acm_index()
            or config.MULTICLUSTER["primary_cluster"]
        ):
            join_cmd = (
                f"join --kubeconfig {get_kube_config_path(cluster.ENV_DATA['cluster_path'])} "
                f"{config.MULTICLUSTER['submariner_info_file']} "
                f"--clusterid c{self.cluster_seq}"
            )
            run_subctl_cmd(
                join_cmd,
            )
            logger.info(f"Subctl join succeeded for {cluster.ENV_DATA['cluster_name']}")
            self.cluster_seq = self.cluster_seq + 1
            self.dr_only_list.append(cluster_index)

    @retry(CommandFailed, tries=5, delay=30, backoff=1)
    def deploy_broker(self):
        # Deploy broker on designated cluster
        # follow this config switch statement carefully to be mindful
        # about the context with which we are performing the operations
        config.switch_ctx(self.designated_broker_cluster_index)
        logger.info(f"Switched context: {config.cluster_ctx.ENV_DATA['cluster_name']}")
        delete_file_with_prefix("broker-info.subm")
        # FIX: Add explicit --kubeconfig parameter (follows official Submariner docs)
        kubeconfig_path = get_kube_config_path(config.cluster_ctx.ENV_DATA['cluster_path'])
        deploy_broker_cmd = f"deploy-broker --kubeconfig {kubeconfig_path}"
        run_subctl_cmd(deploy_broker_cmd)

    @retry(CommandFailed, tries=5, delay=30, backoff=1)
    def prepare_aws_cloud(self, cluster):
        infra_id = get_infra_id(cluster.ENV_DATA["cluster_path"])
        # FIX: Add explicit --kubeconfig parameter (follows official Submariner docs)
        kubeconfig_path = get_kube_config_path(cluster.ENV_DATA['cluster_path'])
        prepare_cmd = f'cloud prepare aws --kubeconfig {kubeconfig_path} --ocp-metadata {cluster.ENV_DATA["cluster_path"]}/metadata.json --region {cluster.ENV_DATA["region"]}'
        run_subctl_cmd(prepare_cmd)

    @retry(CommandFailed, tries=5, delay=60, backoff=1)
    def verify_connections(self):
        for i in self.dr_only_list:
            kube_config_path = get_kube_config_path(
                config.clusters[i].ENV_DATA["cluster_path"]
            )
            connect_check = f"show connections --kubeconfig {kube_config_path}"
            run_subctl_cmd(connect_check)

    def submariner_configure_upstream(self):
        """
        Deploy and Configure upstream submariner
        Raises:
            DRPrimaryNotFoundException: If there is no designated primary cluster found
        """
        if self.designated_broker_cluster_index < 0:
            raise DRPrimaryNotFoundException("Designated primary cluster not found")

        self.deploy_broker()
        create_aws_policy()
        restore_index = config.cur_index
        for cluster in get_non_acm_cluster_config(True):
            config.switch_ctx(cluster.MULTICLUSTER["multicluster_index"])
            assign_aws_policy(cluster.ENV_DATA["cluster_name"])
            try:
                self.prepare_aws_cloud(cluster)
                self.join_cluster(cluster)
            except CommandFailed:
                logger.error("Unable to prepare aws cloud for submariner")
                raise
        config.switch_ctx(restore_index)
        # verify command throws error
        self.verify_connections()

    def get_primary_cluster_index(self):
        """
        Return list index (in the config list) of the primary cluster
        A cluster is primary from DR perspective
        Returns:
            int: Index of the cluster designated as primary
        """
        for i in range(len(config.clusters)):
            if config.clusters[i].MULTICLUSTER.get("primary_cluster"):
                return i
        return -1

    def get_default_gateway_node(self):
        """
        Return the default node to be used as submariner gateway
        Returns:
            str: Name of the gateway node
        """
        # Always return the first worker node
        return get_typed_worker_nodes()[0]
