import requests
import json
import logging
import os
import platform
import yaml
import shutil
import time

from semantic_version import Version

from src.framework import config
from src.utility.constants import (
    TOP_DIR,
    AUTH_CONFIG_DOCS,
    AUTHYAML,
    EXTERNAL_DIR,
    WORKER_MACHINE,
    MASTER_MACHINE,
    MACHINECONFIGPOOL,
)
from src.utility.exceptions import (
    UnsupportedOSType,
    ClientDownloadError,
    EmailPasswordNotFoundException,
    CommandFailed,
    ResourceWrongStatusException,
    UnknownCloneTypeException,
)
from src.utility.cmd import exec_cmd
from src.utility.retry import retry

logger = logging.getLogger(__name__)


def download_installer(
    version=None,
    bin_dir=None,
    force_download=False,
    verify_ssl_certificate=True,
):
    version = version or config.DEPLOYMENT["installer_version"]
    bin_dir = os.path.expanduser(bin_dir or config.RUN["bin_dir"])
    installer_filename = "openshift-install"
    installer_binary_path = os.path.join(bin_dir, installer_filename)

    # Expose the full version (e.g., 4.2.0-0.nightly -> 4.2.0-0.nightly-2019-08-08-103722)
    version = expose_ocp_version(version)

    # Create a versioned cache directory for installers
    cache_dir = os.path.join(bin_dir, "installer_cache")
    cached_installer_path = os.path.join(cache_dir, f"openshift-install-{version}")

    # Handle force download - only delete cached version, keep existing installer as fallback
    # The existing installer will be overwritten after successful download
    if force_download and config.cur_index == 0:
        if os.path.isfile(cached_installer_path):
            delete_file(cached_installer_path)

    # Check if we have this version cached
    if os.path.isfile(cached_installer_path):
        logger.info(f"Using cached installer for version {version}.")
        # Remove existing binary first to avoid macOS inode flagging
        # (a SIGKILL'd binary's inode stays flagged if overwritten in-place)
        if os.path.isfile(installer_binary_path):
            os.remove(installer_binary_path)
        shutil.copy2(cached_installer_path, installer_binary_path)
    elif os.path.isfile(installer_binary_path):
        # Check if existing installer matches requested version
        try:
            existing_version = get_installer_version(installer_binary_path)
            if existing_version and version in existing_version:
                logger.debug(f"Installer exists ({installer_binary_path}) and matches version {version}, skipping download.")
            else:
                logger.info(f"Existing installer version ({existing_version}) does not match requested version ({version}).")
                _download_with_fallback(
                    version, bin_dir, installer_filename, cached_installer_path,
                    cache_dir, verify_ssl_certificate, installer_binary_path
                )
        except Exception as e:
            logger.warning(f"Unable to determine existing installer version: {e}. Re-downloading.")
            _download_with_fallback(
                version, bin_dir, installer_filename, cached_installer_path,
                cache_dir, verify_ssl_certificate, installer_binary_path
            )
    else:
        _download_with_fallback(
            version, bin_dir, installer_filename, cached_installer_path,
            cache_dir, verify_ssl_certificate, installer_binary_path
        )

    installer_version = exec_cmd(f"{installer_binary_path} version")
    logger.info(f"OpenShift Installer version: {installer_version}")
    return installer_binary_path


def _find_fallback_installer(cache_dir, requested_version, bin_dir=None):
    """
    Find a fallback installer from cache when download fails.
    Prefers versions matching the same major.minor version, sorted by modification time (newest first).
    Also checks for existing installer in bin_dir as a last resort.

    Args:
        cache_dir (str): Path to the installer cache directory
        requested_version (str): The originally requested version string
        bin_dir (str): Path to bin directory to check for existing installer

    Returns:
        str: Path to the best fallback installer, or None if no cached installers exist
    """
    cached_installers = []

    # Check cache directory for versioned installers
    if os.path.exists(cache_dir):
        for filename in os.listdir(cache_dir):
            if filename.startswith("openshift-install-"):
                filepath = os.path.join(cache_dir, filename)
                if os.path.isfile(filepath):
                    mtime = os.path.getmtime(filepath)
                    cached_installers.append((filepath, filename, mtime))

    # Extract major.minor version from requested version (e.g., "4.21" from "4.21.0-0.nightly-...")
    major_minor = None
    try:
        parts = requested_version.split(".")
        if len(parts) >= 2:
            major_minor = f"{parts[0]}.{parts[1]}"
    except Exception:
        pass

    # Sort by modification time (newest first)
    if cached_installers:
        cached_installers.sort(key=lambda x: x[2], reverse=True)

        # First, try to find a matching major.minor version
        if major_minor:
            for filepath, filename, _ in cached_installers:
                version_part = filename.replace("openshift-install-", "")
                if version_part.startswith(major_minor):
                    return filepath

        # If no major.minor match, return the most recently modified cached installer
        return cached_installers[0][0]

    # Fallback: check for existing installer binary in bin_dir (from previous runs before caching was added)
    if bin_dir:
        existing_installer = os.path.join(bin_dir, "openshift-install")
        if os.path.isfile(existing_installer):
            logger.info(f"Found existing installer at {existing_installer}, using as fallback.")
            return existing_installer

    return None


def _download_with_fallback(
    version, bin_dir, installer_filename, cached_installer_path,
    cache_dir, verify_ssl_certificate, installer_binary_path
):
    """
    Attempt to download the installer, falling back to a cached version on failure.

    Args:
        version (str): Version to download
        bin_dir (str): Path to bin directory
        installer_filename (str): Name of the installer binary
        cached_installer_path (str): Path to store the cached installer
        cache_dir (str): Path to the cache directory
        verify_ssl_certificate (bool): Whether to verify SSL certificates
        installer_binary_path (str): Path to the main installer binary
    """
    try:
        _download_installer_binary(
            version, bin_dir, installer_filename, cached_installer_path,
            cache_dir, verify_ssl_certificate
        )
        shutil.copy2(cached_installer_path, installer_binary_path)
    except Exception as e:
        logger.warning(f"Failed to download installer version {version}: {e}")
        fallback_path = _find_fallback_installer(cache_dir, version, bin_dir)
        if fallback_path:
            fallback_version = os.path.basename(fallback_path).replace("openshift-install-", "")
            logger.warning(
                f"Falling back to cached installer: {fallback_version}. "
                f"Note: This may differ from the requested version {version}."
            )
            # Only copy if fallback is different from installer_binary_path
            if os.path.abspath(fallback_path) != os.path.abspath(installer_binary_path):
                shutil.copy2(fallback_path, installer_binary_path)
        else:
            logger.error("No cached installer available for fallback.")
            raise


def get_installer_version(installer_binary_path):
    """
    Get version reported by `openshift-install version`.

    Args:
        installer_binary_path (str): path to `openshift-install` binary

    Returns:
        str: version string reported by the installer.
            None if the installer does not exist or version cannot be determined.
    """
    if os.path.isfile(installer_binary_path):
        try:
            result = exec_cmd(f"{installer_binary_path} version")
            # Parse version from output like "openshift-install 4.21.0-0.nightly-2026-03-01-213049"
            version_output = result.stdout.decode() if hasattr(result, 'stdout') else str(result)
            for line in version_output.split('\n'):
                if 'openshift-install' in line.lower():
                    parts = line.split()
                    if len(parts) >= 2:
                        return parts[1]
            return version_output.strip()
        except Exception:
            return None
    return None


def _download_installer_binary(
    version, bin_dir, installer_filename, cached_installer_path,
    cache_dir, verify_ssl_certificate
):
    """
    Internal function to download the installer binary and cache it.

    Args:
        version (str): Version to download
        bin_dir (str): Path to bin directory
        installer_filename (str): Name of the installer binary
        cached_installer_path (str): Path to store the cached installer
        cache_dir (str): Path to the cache directory
        verify_ssl_certificate (bool): Whether to verify SSL certificates
    """
    logger.info(f"Downloading openshift installer ({version}).")
    prepare_bin_dir()

    # Ensure cache directory exists
    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir)
        logger.info(f"Created installer cache directory: {cache_dir}")

    # record current working directory and switch to BIN_DIR
    previous_dir = os.getcwd()
    os.chdir(bin_dir)

    try:
        if "nightly" in version:
            pull_secret_path = os.path.join(TOP_DIR, "data", "pull-secret")
            cmd = (
                f"oc adm release extract --registry-config {pull_secret_path} --command={installer_filename} "
                f"--to ./ registry.ci.openshift.org/ocp/release:{version}"
            )
            exec_cmd(cmd)
        else:
            tarball = f"{installer_filename}.tar.gz"
            url = get_openshift_mirror_url(installer_filename, version)
            download_file(url, tarball, verify=verify_ssl_certificate)
            exec_cmd(f"tar xzvf {tarball} {installer_filename}")
            delete_file(tarball)

        # Cache the downloaded installer
        downloaded_path = os.path.join(bin_dir, installer_filename)
        if os.path.isfile(downloaded_path):
            shutil.copy2(downloaded_path, cached_installer_path)
            logger.info(f"Cached installer for version {version} at {cached_installer_path}")
    finally:
        # return to the previous working directory
        os.chdir(previous_dir)


def get_openshift_client(
    version=None, bin_dir=None, force_download=False, skip_comparison=False
):
    """
    Download the OpenShift client binary, if not already present.
    Update env. PATH and get path of the oc binary.
    Args:
        version (str): Version of the client to download
            (default: config.RUN['client_version'])
        bin_dir (str): Path to bin directory (default: config.RUN['bin_dir'])
        force_download (bool): Force client download even if already present
        skip_comparison (bool): Skip the comparison between the existing OCP client
            version and the configured one.
    Returns:
        str: Path to the client binary
    """
    version = version or config.RUN["client_version"]
    bin_dir = os.path.expanduser(bin_dir or config.RUN["bin_dir"])
    client_binary_path = os.path.join(bin_dir, "oc")
    download_client = True
    client_version = None
    try:
        version = expose_ocp_version(version)
    except Exception:
        logger.exception("Unable to expose OCP version, skipping client download.")
        skip_comparison = True
        download_client = False
        force_download = False
    if force_download:
        logger.info("Forcing client download.")
    elif os.path.isfile(client_binary_path) and not skip_comparison:
        current_client_version = get_client_version(client_binary_path)
        if current_client_version != version:
            logger.info(
                f"Existing client version ({current_client_version}) does not match "
                f"configured version ({version})."
            )
        else:
            logger.debug(
                f"Client exists ({client_binary_path}) and matches configured version, "
                f"skipping download."
            )
            download_client = False
    if download_client:
        # Move existing client binaries to backup location
        client_binary_backup = f"{client_binary_path}.bak"

        try:
            os.rename(client_binary_path, client_binary_backup)
        except FileNotFoundError:
            pass

        # Download the client
        logger.info(f"Downloading openshift client ({version}).")
        prepare_bin_dir()
        # record current working directory and switch to BIN_DIR
        previous_dir = os.getcwd()
        os.chdir(bin_dir)
        url = get_openshift_mirror_url("openshift-client", version)
        tarball = "openshift-client.tar.gz"
        download_file(url, tarball)
        exec_cmd(f"tar xzvf {tarball} oc kubectl")
        delete_file(tarball)

        try:
            client_version = exec_cmd(f"{client_binary_path} version --client")
        except CommandFailed:
            logger.error("Unable to get version from downloaded client.")

        if client_version:
            try:
                delete_file(client_binary_backup)
                logger.info("Deleted backup binaries.")
            except FileNotFoundError:
                pass
        else:
            try:
                os.rename(client_binary_backup, client_binary_path)
                logger.info("Restored backup binaries to their original location.")
            except FileNotFoundError:
                raise ClientDownloadError(
                    "No backups exist and new binary was unable to be verified."
                )

        # return to the previous working directory
        os.chdir(previous_dir)

    logger.info(f"OpenShift Client version: {client_version}")
    return client_binary_path


def expose_ocp_version(version):
    """
    This helper function exposes latest nightly version or GA version of OCP.
    When the version string ends with .nightly (e.g. 4.2.0-0.nightly) it will
    expose the version to latest accepted OCP build
    (e.g. 4.2.0-0.nightly-2019-08-08-103722)
    If the version ends with -ga than it will find the latest GA OCP version
    and will expose 4.2-ga to for example 4.2.22.

    Set OCP_INSTALLER_VERSION to override nightly resolution with a specific
    cached version (e.g. 4.22.0-0.nightly-2026-05-26-073129).

    Args:
        version (str): Verison of OCP
    Returns:
        str: Version of OCP exposed to full version if latest nighly passed
    """
    override = os.environ.get("OCP_INSTALLER_VERSION")
    if override:
        logger.info(
            f"OCP_INSTALLER_VERSION override set: using '{override}' "
            f"instead of resolving '{version}'"
        )
        return override
    if version.endswith(".nightly"):
        latest_nightly_url = (
            f"https://amd64.ocp.releases.ci.openshift.org/api/v1/"
            f"releasestream/{version}/latest"
        )
        version_url_content = get_url_content(latest_nightly_url)
        version_json = json.loads(version_url_content)
        return version_json["name"]
    if version.endswith("-ga"):
        channel = config.DEPLOYMENT.get("ocp_channel", "stable")
        ocp_version = version.rstrip("-ga")
        index = config.DEPLOYMENT.get("ocp_version_index", -1)
        return get_latest_ocp_version(f"{channel}-{ocp_version}", index)
    else:
        return version


def get_available_ocp_versions(channel):
    """
    Find all available OCP versions for specific channel.
    Args:
        channel (str): Channel of OCP (e.g. stable-4.2 or fast-4.2)
    Returns
        list: Sorted list with OCP versions for specified channel.
    """
    headers = {"Accept": "application/json"}
    req = requests.get(
        "https://api.openshift.com/api/upgrades_info/v1/graph?channel={channel}".format(
            channel=channel
        ),
        headers=headers,
    )
    data = req.json()
    versions = [Version(node["version"]) for node in data["nodes"]]
    versions.sort()
    return versions


def get_latest_ocp_version(channel, index=-1):
    """
    Find latest OCP version for specific channel.
    Args:
        channel (str): Channel of OCP (e.g. stable-4.2 or fast-4.2)
        index (int): Index to get from all available versions list
            e.g. default -1 is latest version (version[-1]). If you want to get
            previous version pass index -2 and so on.
    Returns
        str: Latest OCP version for specified channel.
    """
    versions = get_available_ocp_versions(channel)
    return str(versions[index])


def get_url_content(url, **kwargs):
    """
    Return URL content
    Args:
        url (str): URL address to return
        kwargs (dict): additional keyword arguments passed to requests.get(...)
    Returns:
        str: Content of URL
    Raises:
        AssertionError: When couldn't load URL
    """
    logger.debug(f"Download '{url}' content.")
    r = requests.get(url, **kwargs)
    assert r.ok, f"Couldn't load URL: {url} content! Status: {r.status_code}."
    return r.content


def delete_file(file_name):
    """
    Delete file_name
    Args:
        file_name (str): Path to the file you want to delete
    """
    os.remove(file_name)


def delete_file_with_prefix(prefix):
    """
    Delete all files with prefix
    Args:
        prefix (str): Prefix to the files you want to delete
    """
    try:
        for file in os.listdir("."):
            if os.path.isfile(file) and file.startswith(prefix):
                delete_file(file)
    except FileNotFoundError:
        pass


def prepare_bin_dir(bin_dir=None):
    """
    Prepare bin directory for OpenShift client and installer
    Args:
        bin_dir (str): Path to bin directory (default: config.RUN['bin_dir'])
    """
    bin_dir = os.path.expanduser(bin_dir or config.RUN["bin_dir"])
    try:
        os.mkdir(bin_dir)
        logger.info(f"Directory '{bin_dir}' successfully created.")
    except FileExistsError:
        logger.debug(f"Directory '{bin_dir}' already exists.")


def get_openshift_mirror_url(file_name, version):
    """
    Format url to OpenShift mirror (for client and installer download).
    Args:
        file_name (str): Name of file
        version (str): Version of the installer or client to download
    Returns:
        str: Url of the desired file (installer or client)
    Raises:
        UnsupportedOSType: In case the OS type is not supported
        UnavailableBuildException: In case the build url is not reachable
    """
    if platform.system() == "Darwin":
        os_type = "mac"
    elif platform.system() == "Linux":
        os_type = "linux"
    else:
        raise UnsupportedOSType
    mirror_link = (
        config.DEPLOYMENT.get("ocp_mirror_url", "").find("mirror.openshift.com") != -1
    )
    if mirror_link:
        url_template = os.path.join(
            config.DEPLOYMENT.get("ocp_mirror_url", ""),
            "{version}/{file_name}-{os_type}.tar.gz",
        )
    else:
        url_template = os.path.join(
            config.DEPLOYMENT.get("ocp_mirror_url", ""),
            "{version}/{file_name}-{os_type}-{version}.tar.gz",
        )
    url = url_template.format(
        version=version,
        file_name=file_name,
        os_type=os_type,
    )
    logger.info(f"openshift installer url: {url}")
    return url


@retry(ResourceWrongStatusException, tries=4, delay=5, backoff=1)
def download_file(url, filename, **kwargs):
    """
    Download a file from a specified url
    Args:
        url (str): URL of the file to download
        filename (str): Name of the file to write the download to
        kwargs (dict): additional keyword arguments passed to requests.get(...)
    """
    logger.debug(f"Download '{url}' to '{filename}'.")
    with open(filename, "wb") as f:
        r = requests.get(url, **kwargs)
        if not r.ok:
            print(
                f"The URL {url} is not available! Status: {r.status_code}. retrying..."
            )
            raise ResourceWrongStatusException(
                f"The URL {url} is not available! Status: {r.status_code}."
            )

        assert r.ok, f"The URL {url} is not available! Status: {r.status_code}."
        f.write(r.content)


def get_client_version(client_binary_path):
    """
    Get version reported by `oc version`.
    Args:
        client_binary_path (str): path to `oc` binary
    Returns:
        str: version reported by `oc version`.
            None if the client does not exist at the provided path.
    """
    if os.path.isfile(client_binary_path):
        cmd = f"{client_binary_path} version --client -o json"
        resp = exec_cmd(cmd)
        stdout = json.loads(resp.stdout.decode())
        return stdout["releaseClientVersion"]


def ocp4mcoci_log_path():
    """
    Construct the full path for the log directory.
    Returns:
        str: full path for ocp4mco-ci log directory
    """
    return os.path.expanduser(
        os.path.join(config.RUN["log_dir"], f"ocp4mco-ci-logs-{config.RUN['run_id']}")
    )


def add_path_to_env_path(path):
    """
    Add path to the PATH environment variable (if not already there).
    Args:
        path (str): Path which should be added to the PATH env. variable
    """
    env_path = os.environ["PATH"].split(os.pathsep)
    if path not in env_path:
        os.environ["PATH"] = os.pathsep.join([path] + env_path)
        logger.info(f"Path '{path}' added to the PATH environment variable.")
    logger.debug(f"PATH: {os.environ['PATH']}")


def create_directory_path(path):
    """
    Creates directory if path doesn't exists
    """
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        os.makedirs(path)
    else:
        logger.debug(f"{path} already exists")


def is_cluster_running(cluster_path):
    from src.utility.openshift_ops import OpenshiftOps

    return OpenshiftOps.set_kubeconfig(get_kube_config_path(cluster_path))


def get_kube_config_path(cluster_path=""):
    return os.path.join(cluster_path, config.RUN.get("kubeconfig_location"))


def get_email_pass():
    email_pass_path = os.path.join(TOP_DIR, "data", "email-pass")
    is_exist = os.path.exists(email_pass_path)
    if not is_exist:
        raise EmailPasswordNotFoundException(
            f"Email password does not exists on path: {email_pass_path}."
        )
    with open(email_pass_path, "r") as f:
        # single string
        return f.read()


def get_ocp_version(seperator=None):
    """
    Get current ocp version
    Args:
        seperator (str): String that would seperate major and
            minor version nubers
    Returns:
        string : If seperator is 'None', version string will be returned as is
            eg: '4.2', '4.3'.
            If seperator is provided then '.' in the version string would be
            replaced by seperator and resulting string will be returned.
            eg: If seperator is '_' then string returned would be '4_2'
    """
    version = ""
    try:
        char = seperator if seperator else "."
        prefixes = ("latest", "candidate", "fast", "stable")
        if config.ENV_DATA.get("skip_ocp_deployment"):
            raw_version = json.loads(exec_cmd("oc version -o json").stdout)[
                "openshiftVersion"
            ]
        else:
            raw_version = config.DEPLOYMENT["installer_version"]
        if raw_version.startswith(prefixes):
            version = raw_version.split("-")[1]
        else:
            version = Version.coerce(raw_version)
            version = char.join([str(version.major), str(version.minor)])
    except CommandFailed:
        logger.error("Unable to get version OCP version.")
    return version


def load_auth_config():
    """
    Load the authentication config YAML from /data/auth.yaml

    Raises:
        FileNotFoundError: if the auth config is not found

    Returns:
        dict: A dictionary reprensenting the YAML file

    """
    logger.info("Retrieving the authentication config dictionary")
    auth_file = os.path.join(TOP_DIR, "data", AUTHYAML)
    try:
        with open(auth_file) as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logger.warning(
            f"Unable to find the authentication configuration at {auth_file}, "
            f"please refer to the getting started guide ({AUTH_CONFIG_DOCS})"
        )
        return {}


def clone_repo(
    url,
    location,
    branch="master",
    to_checkout=None,
    clone_type="shallow",
    force_checkout=False,
):
    """
    Clone a repository or checkout latest changes if it already exists at
        specified location.

    Args:
        url (str): location of the repository to clone
        location (str): path where the repository will be cloned to
        branch (str): branch name to checkout
        to_checkout (str): commit id or tag to checkout
        clone_type (str): type of clone (shallow, blobless, treeless and normal)
            By default, shallow clone will be used. For normal clone use
            clone_type as "normal".
        force_checkout (bool): True for force checkout to branch.
            force checkout will ignore the unmerged entries.

    Raises:
        UnknownCloneTypeException: In case of incorrect clone_type is used

    """
    if clone_type == "shallow":
        if branch != "master":
            git_params = "--no-single-branch --depth=1"
        else:
            git_params = "--depth=1"
    elif clone_type == "blobless":
        git_params = "--filter=blob:none"
    elif clone_type == "treeless":
        git_params = "--filter=tree:0"
    elif clone_type == "normal":
        git_params = ""
    else:
        raise UnknownCloneTypeException
    """
    Workaround as a temp solution since sno installer git is different from ocp installer if directory already exist
    it checks if the repo already exist from SNO but the git is OCP it delete the installer directory and
    the other way around
    """
    installer_path_exist = os.path.isdir(location)
    if ("installer" in location) and installer_path_exist:
        if "coreos" not in location:
            installer_dir = os.path.join(EXTERNAL_DIR, "installer")
            remote_output = exec_cmd(f"git -C {installer_dir} remote -v")
            if (("srozen" in remote_output) and ("openshift" in url)) or (
                ("openshift" in remote_output) and ("srozen" in url)
            ):
                shutil.rmtree(installer_dir)
                logger.info(
                    f"Waiting for 5 seconds to get all files and folder deleted from {installer_dir}"
                )
                time.sleep(5)

    if not os.path.isdir(location):
        logger.info("Cloning repository into %s", location)
        exec_cmd(f"git clone {git_params} {url} {location}")
    else:
        logger.info("Repository already cloned at %s, skipping clone", location)
        logger.info("Fetching latest changes from repository")
        exec_cmd("git fetch --all", cwd=location)
    logger.info("Checking out repository to specific branch: %s", branch)
    if force_checkout:
        exec_cmd(f"git checkout --force {branch}", cwd=location)
    else:
        exec_cmd(f"git checkout {branch}", cwd=location)
    logger.info("Reset branch: %s with latest changes", branch)
    exec_cmd(f"git reset --hard origin/{branch}", cwd=location)
    if to_checkout:
        exec_cmd(f"git checkout {to_checkout}", cwd=location)


def get_non_acm_cluster_config(include_acm=False):
    """
    Get a list of non-acm cluster's config objects
    Returns:
        list: of cluster config objects
    """
    non_acm_list = []
    for i in range(len(config.clusters)):
        conf = config.clusters[i]
        if i == config.get_acm_index() and (
            not conf.MULTICLUSTER["primary_cluster"] or not include_acm
        ):
            continue
        else:
            non_acm_list.append(config.clusters[i])
    return non_acm_list


def get_kube_config(cluster_path):
    kube_config_path = get_kube_config_path(cluster_path)
    with open(kube_config_path, "r") as f:
        return f.read()


def wait_for_machineconfigpool_status(node_type, timeout=900, skip_tls_verify=False):
    """
    Check for Machineconfigpool status

    Args:
        node_type (str): The node type to check machineconfigpool
            status is updated.
            e.g: worker, master and all if we want to check for all nodes
        timeout (int): Time in seconds to wait
        skip_tls_verify (bool): True if allow skipping TLS verification

    """
    logger.info("Sleeping for 60 sec to start update machineconfigpool status")
    time.sleep(60)
    # importing here to avoid dependencies
    from src.ocs import ocp

    node_types = [node_type]
    if node_type == "all":
        node_types = [f"{WORKER_MACHINE}", f"{MASTER_MACHINE}"]

    for role in node_types:
        logger.info(f"Checking machineconfigpool status for {role} nodes")
        ocp_obj = ocp.OCP(
            kind=MACHINECONFIGPOOL,
            resource_name=role,
            skip_tls_verify=skip_tls_verify,
        )
        machine_count = ocp_obj.get()["status"]["machineCount"]

        assert ocp_obj.wait_for_resource(
            condition=str(machine_count),
            column="READYMACHINECOUNT",
            timeout=timeout,
            sleep=5,
        )


def get_cluster_metadata(cluster_path):
    meta_data_json_path = f"{cluster_path}/metadata.json"
    try:
        f = open(meta_data_json_path, "r")
        meta_data_json = json.load(f)
        return meta_data_json
    except IOError as error:
        logger.error(f"Unable to find infra id {meta_data_json_path}")
        raise error


def get_primary_cluster_config():
    """
    Get the primary cluster config object in a DR scenario

    Return:
        framework.config: primary cluster config object from config.clusters

    """
    for cluster in config.clusters:
        if cluster.MULTICLUSTER["primary_cluster"]:
            return cluster
