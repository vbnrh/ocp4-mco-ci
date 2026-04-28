import os
import logging
import json
import yaml
import shutil

from src.utility.retry import retry
from src.framework import config
from src.utility import utils
from src.utility import constants
from src.utility.exceptions import PullSecretNotFoundException, CommandFailed
from src.utility import templating

logger = logging.getLogger(__name__)


class OCPDeployment:
    def __init__(self, cluster_name, cluster_path):
        self.cluster_name = cluster_name
        self.cluster_path = cluster_path
        self.installer_binary_path = ""

    def deploy_prereq(self):
        # download openshift installer
        self.installer_binary_path = self.download_installer()
        # create config
        self.create_config()

    @retry(CommandFailed, tries=5, delay=60, backoff=1)
    def download_installer(self):
        return utils.download_installer(
            version=config.DEPLOYMENT["installer_version"],
            bin_dir=config.RUN["bin_dir"],
            force_download=config.DEPLOYMENT["force_download_installer"],
            verify_ssl_certificate=config.RUN["https_certification_verification"],
        )

    def get_pull_secret(self):
        """
        Load pull secret file
        Returns:
            dict: content of pull secret
        """
        pull_secret_path = os.path.join(constants.TOP_DIR, "data", "pull-secret")
        is_exist = os.path.exists(pull_secret_path)
        if not is_exist:
            raise PullSecretNotFoundException(
                f"Pull secret does not exists on path: {pull_secret_path}."
            )
        with open(pull_secret_path, "r") as f:
            # Parse, then unparse, the JSON file.
            # We do this for two reasons: to ensure it is well-formatted, and
            # also to ensure it ends up as a single line.
            return json.dumps(json.loads(f.read()))

    def get_ssh_key(self):
        """
        Loads public ssh to be used for deployment
        Returns:
            str: public ssh key or empty string if not found
        """
        ssh_key = os.path.expanduser(config.DEPLOYMENT.get("ssh_key"))
        if not os.path.isfile(ssh_key):
            return ""
        with open(ssh_key, "r") as fs:
            lines = fs.readlines()
            return lines[0].rstrip("\n") if lines else ""

    def create_config(self):
        """
        Create the OCP deploy config
        """
        deployment_platform = config.ENV_DATA["platform"]
        # Generate install-config from template
        logger.info("Generating install-config")
        _templating = templating.Templating()
        ocp_install_template = f"install-config-{deployment_platform.lower()}.yaml.j2"
        ocp_install_template_path = os.path.join(ocp_install_template)
        install_config_str = _templating.render_template(
            ocp_install_template_path, config.ENV_DATA
        )
        # Log the install-config *before* adding the pull secret,
        # so we don't leak sensitive data.
        logger.info(f"Install config: \n{install_config_str}")
        # Parse the rendered YAML so that we can manipulate the object directly
        install_config_obj = yaml.safe_load(install_config_str)
        install_config_obj["pullSecret"] = self.get_pull_secret()
        ssh_key = self.get_ssh_key()
        if ssh_key:
            install_config_obj["sshKey"] = ssh_key
        install_config_str = yaml.safe_dump(install_config_obj)
        install_config_path = os.path.join(self.cluster_path, "install-config.yaml")
        # create cluster directory
        if not os.path.exists(self.cluster_path):
            os.mkdir(self.cluster_path)
        logger.info(f"Install directory: {self.cluster_path} is created successfully")
        with open(install_config_path, "w") as f:
            f.write(install_config_str)

    @staticmethod
    def deploy_ocp(installer_binary_path, cluster_path, log_cli_level="INFO"):
        # Do not access framework.config directly inside deploy_ocp, it is not thread safe
        try:
            # 4.22+ nightlies require opting out of sigstore image signing verification
            os.environ["OPENSHIFT_INSTALL_EXPERIMENTAL_DISABLE_IMAGE_POLICY"] = "true"
            utils.exec_cmd(
                cmd="{bin_dir} create cluster --dir {cluster_dir} --log-level={log_level}".format(
                    bin_dir=installer_binary_path,
                    cluster_dir=cluster_path,
                    log_level=log_cli_level,
                ),
                timeout=3600,
            )
        except CommandFailed as ex:
            logger.error("Unable to deploy ocp cluster.")
