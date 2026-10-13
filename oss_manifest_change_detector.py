from github import Github, Auth
from datetime import datetime, timedelta, timezone
import os
import logging
import sys
import requests
import oss_ci_config as occ

logging.basicConfig(level=logging.INFO)

ORG = occ.GITHUB_ORG


class OSSLayerChangeDetector:
    """Detects commits on tracked rdkcentral OSS layer repositories in the past 24 hours."""

    def __init__(self, layer_repo, branch, github_token):
        self.layer_repo = layer_repo
        self.branch     = branch
        self.g          = Github(auth=Auth.Token(github_token))
        self.repo       = self.g.get_repo("{}/{}".format(ORG, layer_repo))
        self.branch_obj = self.repo.get_branch(branch)

    def get_recent_commits(self):
        since = datetime.now(timezone.utc) - timedelta(days=1)
        logging.info("Checking commits in {}/{}@{} since {}".format(
            ORG, self.layer_repo, self.branch, since))
        commits = self.repo.get_commits(sha=self.branch_obj.commit.sha, since=since)
        count = len(commits.get_page(0))
        logging.info("Found {} commit(s) in {}/{} in the past 24 hours.".format(
            count, ORG, self.layer_repo))
        return count

    def get_latest_commit_sha(self):
        return self.branch_obj.commit.sha


def trigger_dispatch(target_repo, layer_name, latest_sha, manifest_type, token):
    url = "https://api.github.com/repos/{}/{}/dispatches".format(ORG, target_repo)
    headers = {
        "Authorization": "token {}".format(token),
        "Accept": "application/vnd.github+json"
    }
    payload = {
        "event_type": "oss-layer-change-detected",
        "client_payload": {
            "layer_name":    layer_name,
            "new_sha":       latest_sha,
            "source_org":    ORG,
            "manifest_type": manifest_type,
            "triggered_by":  "oss_manifest_change_detector"
        }
    }
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        if response.status_code == 204:
            logging.info("repository_dispatch triggered on {}/{} for layer '{}' "
                         "(manifest_type={}).".format(ORG, target_repo, layer_name, manifest_type))
        else:
            logging.error("Failed to trigger dispatch on {}/{}: {} - {}".format(
                ORG, target_repo, response.status_code, response.text))
    except requests.exceptions.RequestException as e:
        logging.error("Request error triggering dispatch on {}/{}: {}".format(
            ORG, target_repo, str(e)))


def check_layers(layer_jobs, default_branch, token, target_repo, manifest_type, changed):
    for layer_repo, layer_info in layer_jobs.items():
        layer_branch = layer_info.get('monitor_branch', default_branch)
        try:
            detector = OSSLayerChangeDetector(layer_repo, layer_branch, token)
            commit_count = detector.get_recent_commits()
        except Exception as e:
            logging.error("Skipping {}/{}@{}: {}".format(ORG, layer_repo, layer_branch, e))
            continue

        if commit_count > 0:
            latest_sha = detector.get_latest_commit_sha()
            logging.info("Change detected in '{}/{}'. Latest SHA: {}.".format(
                ORG, layer_repo, latest_sha))
            trigger_dispatch(target_repo, layer_repo, latest_sha, manifest_type, token)
            changed.append("{}/{}".format(ORG, layer_repo))
        else:
            logging.info("No recent commits in '{}/{}'. Skipping.".format(ORG, layer_repo))


if __name__ == "__main__":
    token  = os.getenv('RDKCENTRAL_GITHUB_TOKEN') or os.getenv('GITHUB_TOKEN')
    branch = os.getenv('BRANCH', occ.OSS_MONITOR_BRANCH)

    if not token:
        logging.error("RDKCENTRAL_GITHUB_TOKEN / GITHUB_TOKEN environment variable is required.")
        sys.exit(1)

    layers_with_changes = []

    logging.info("=== Checking OSS layers (rdk-oss.xml / rdk-docker.xml) ===")
    check_layers(occ.OSS_LAYER_JOBS, branch, token,
                 occ.OSS_MANIFEST_REPO, 'oss', layers_with_changes)

    logging.info("=== Checking OSS Common layers (rdk-oss-common.xml) ===")
    check_layers(occ.OSS_COMMON_LAYER_JOBS, branch, token,
                 occ.OSS_COMMON_MANIFEST_REPO, 'oss-common', layers_with_changes)

    if layers_with_changes:
        logging.info("OSS CI complete. Dispatched Auto PR for layers: {}".format(layers_with_changes))
    else:
        logging.info("OSS CI complete. No changes detected in any tracked OSS layer.")
