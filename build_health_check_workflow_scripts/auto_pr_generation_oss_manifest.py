import os
import sys
import re
import shutil
import requests

import github
from github import Github, Auth
from git import Repo, GitCommandError

# ── Ensure repo root is in sys.path so oss_ci_config.py can be imported
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import oss_ci_config as occ

GITHUB_ORG               = occ.GITHUB_ORG          # "rdkcentral"
OSS_MANIFEST_REPO        = occ.OSS_MANIFEST_REPO   # "rdke-oss-manifest"
OSS_MANIFEST_XML_FILES   = occ.OSS_MANIFEST_XML_FILES
OSS_COMMON_MANIFEST_REPO = occ.OSS_COMMON_MANIFEST_REPO
OSS_COMMON_MANIFEST_XML  = occ.OSS_COMMON_MANIFEST_XML
OSS_PR_LABELS            = ['auto-update', 'manifest']
OSS_AUTO_MERGE_LABEL     = 'oss-auto-merge'


def fetch_merge_commits(owner, repo, pr_number, github_token):

    url = 'https://api.github.com/graphql'
    repo_without_org = repo.split('/')[-1]
    print("Fetching merge commits from {}/{} for PR #{}".format(owner, repo_without_org, pr_number))
    headers = {'Authorization': 'Bearer {}'.format(github_token)}

    query = """
    query($repoOwner: String!, $repoName: String!, $prNumber: Int!) {
      repository(owner: $repoOwner, name: $repoName) {
        pullRequest(number: $prNumber) {
          __typename
          merged
          mergeCommit { oid }
          repository { nameWithOwner }
          timelineItems(last: 100, itemTypes: [CONNECTED_EVENT, DISCONNECTED_EVENT]) {
            nodes {
              __typename
              ... on ConnectedEvent {
                subject {
                  ... on Issue {
                    __typename
                    number
                    repository { nameWithOwner }
                    timelineItems(last: 100, itemTypes: [CONNECTED_EVENT, DISCONNECTED_EVENT]) {
                      nodes {
                        __typename
                        ... on ConnectedEvent {
                          subject {
                            ... on PullRequest {
                              __typename
                              number
                              merged
                              mergeCommit { oid }
                              repository { nameWithOwner }
                            }
                          }
                        }
                        ... on DisconnectedEvent {
                          subject {
                            ... on PullRequest {
                              __typename
                              number
                              merged
                              mergeCommit { oid }
                              repository { nameWithOwner }
                            }
                          }
                        }
                      }
                    }
                  }
                }
              }
              ... on DisconnectedEvent {
                subject {
                  ... on Issue {
                    __typename
                    number
                    repository { nameWithOwner }
                  }
                }
              }
            }
          }
        }
      }
    }
    """
    variables = {'repoOwner': owner, 'repoName': repo_without_org, 'prNumber': int(pr_number)}
    response = requests.post(url, json={'query': query, 'variables': variables}, headers=headers)
    result = response.json()

    if response.status_code != 200 or 'errors' in result:
        print("GraphQL query failed", result)
        sys.exit(1)

    pr_data = result['data']['repository']['pullRequest']
    if pr_data is None:
        print("PR #{} not found in {}/{}.".format(pr_number, owner, repo_without_org))
        sys.exit(1)

    main_pr_merge_commit = None
    if pr_data.get('merged') and pr_data.get('mergeCommit'):
        main_pr_merge_commit = {
            'repo': pr_data['repository']['nameWithOwner'],
            'sha':  pr_data['mergeCommit']['oid']
        }

    all_events = pr_data['timelineItems']['nodes']

    # Replay issue link state in order (Connected adds, Disconnected removes)
    current_issues = set()
    for e in all_events:
        subject = e.get('subject') or {}
        if subject.get('__typename') != 'Issue':
            continue
        key = (subject['repository']['nameWithOwner'], subject['number'])
        if e['__typename'] == 'ConnectedEvent':
            current_issues.add(key)
        elif e['__typename'] == 'DisconnectedEvent':
            current_issues.discard(key)

    print("current_issues :", current_issues)

    if len(current_issues) > 1:
        print("Error: More than one issue connected to PR. Aborting.")
        sys.exit(1)

    current_issue = next(iter(current_issues)) if current_issues else None
    if current_issue:
        print("ISSUE being processed for current PR:", current_issue)
    else:
        print("No connected issue found.")

    issue_timeline = []
    if current_issue:
        for e in all_events:
            if e['__typename'] == 'ConnectedEvent':
                subject = e.get('subject', {})
                if subject.get('__typename') == 'Issue':
                    if (subject['repository']['nameWithOwner'], subject['number']) == current_issue:
                        issue_timeline = subject.get('timelineItems', {}).get('nodes', [])
                        break

    # Replay PR link state in order (Connected adds, Disconnected removes)
    current_pr_set = set()
    for e in issue_timeline:
        subject = e.get('subject') or {}
        if subject.get('__typename') != 'PullRequest':
            continue
        if not subject.get('merged'):
            continue
        oid = (subject.get('mergeCommit') or {}).get('oid')
        if not oid:
            continue
        key = (subject['repository']['nameWithOwner'], subject['number'], oid)
        if e['__typename'] == 'ConnectedEvent':
            current_pr_set.add(key)
        elif e['__typename'] == 'DisconnectedEvent':
            current_pr_set.discard(key)

    current_prs = sorted(
        [{'repo': r, 'sha': s} for (r, _, s) in current_pr_set],
        key=lambda x: x['sha']
    )

    if not current_prs and main_pr_merge_commit:
        current_prs.append(main_pr_merge_commit)

    print("Final connected PRs:", current_prs)
    return (
        current_prs,
        current_issue[0] if current_issue else None,
        current_issue[1] if current_issue else None
    )


def extract_ticket_number(pr_title):
    match = re.search(r"[A-Z]+-[0-9]+", pr_title or "")
    return match.group(0) if match else "NO-TICKET"


def post_pr_comment(repo_name, pr_number, comment, github_token):
    if not pr_number:
        print("No source PR number available — skipping comment.")
        return
    url = "https://api.github.com/repos/{}/issues/{}/comments".format(repo_name, pr_number)
    headers = {
        "Authorization": "Bearer {}".format(github_token),
        "Accept": "application/vnd.github+json"
    }
    response = requests.post(url, json={"body": comment}, headers=headers)
    if response.status_code == 201:
        print("Comment posted successfully on PR #{}.".format(pr_number))
    else:
        print("Failed to post comment on PR #{}: {} - {}".format(
            pr_number, response.status_code, response.text))


def build_pr_list_description(prs):
    pr_list = "\n\nList of PRs and Repositories Involved:\n"
    for pr in prs:
        pr_list += "- Repository: {}, Merge Commit SHA: {}\n".format(pr['repo'], pr['sha'])
    return pr_list


def ensure_label_exists(repo, label_name, color='FFFFFF'):
    label_list = {label.name for label in repo.get_labels()}
    if label_name not in label_list:
        repo.create_label(name=label_name, color=color)
        print("Label '{}' created.".format(label_name))
    else:
        print("Label '{}' already exists.".format(label_name))


def find_open_pr_for_branch(repo_obj, head_branch):
    """Return open PR for head branch in same org, else None."""
    head_ref = "{}:{}".format(GITHUB_ORG, head_branch)
    prs = repo_obj.get_pulls(state='open', head=head_ref, sort='created', direction='desc')
    for pr in prs:
        return pr
    return None


def ensure_branch_checked_out(repo, branch_name, base_branch):
    """
    Ensure local checkout is on branch_name.
    - If remote branch exists, check it out and pull (resume mode).
    - Else create from base_branch.
    Returns: (ok: bool, mode: 'resumed'|'created'|'error')
    """
    try:
        repo.git.fetch('origin')
        remote_branches = repo.git.branch('-r')

        if 'origin/{}'.format(branch_name) in remote_branches:
            repo.git.checkout('-B', branch_name, 'origin/{}'.format(branch_name))
            repo.git.pull('origin', branch_name)
            print("Resuming existing branch: {}".format(branch_name))
            return True, 'resumed'

        repo.git.checkout(base_branch)
        repo.git.pull('origin', base_branch)
        repo.git.checkout('-b', branch_name)
        print("Created new branch: {}".format(branch_name))
        return True, 'created'
    except GitCommandError as e:
        print("Error preparing branch {}: {}".format(branch_name, str(e)))
        return False, 'error'


def commit_and_push(manifest_repo_path, commit_message):
    repo = Repo(manifest_repo_path)
    if repo.is_dirty():
        repo.git.config('user.email', os.getenv("SVC_ACCOUNT_EMAIL"))
        repo.git.config('user.name', os.getenv("SVC_ACCOUNT_USERNAME"))
        repo.git.commit('-m', commit_message)
        repo.git.push('--set-upstream', 'origin', repo.active_branch.name)
    else:
        print("No changes to commit.")


def clone_manifest_repo(github_token, repo_name, workspace_dir):
    try:
        repo_path = os.path.join(workspace_dir, "{}_{}".format(GITHUB_ORG, repo_name))
        if os.path.exists(repo_path):
            shutil.rmtree(repo_path)
        clone_url = "https://{}@github.com/{}/{}.git".format(github_token, GITHUB_ORG, repo_name)
        repo = Repo.clone_from(clone_url, repo_path)
        repo.git.config('user.name', os.getenv("SVC_ACCOUNT_USERNAME"))
        repo.git.config('user.email', os.getenv("SVC_ACCOUNT_EMAIL"))
        return repo_path, repo
    except Exception as e:
        print("Failed to clone {}/{}: {}".format(GITHUB_ORG, repo_name, str(e)))
        return None, None


def create_oss_pull_request(github_token, repo_name, head_branch, base_branch,
                            title, description, layer_name, new_revision, source_pr_url):
    g = Github(auth=Auth.Token(github_token))
    full_repo_name = "{}/{}".format(GITHUB_ORG, repo_name)
    repo = g.get_repo(full_repo_name)

    for label in OSS_PR_LABELS:
        ensure_label_exists(repo, label, color='0075ca')
    ensure_label_exists(repo, OSS_AUTO_MERGE_LABEL, color='008672')

    pr_body = (
        "{}\n\n---\n"
        "**Layer:** `{}`\n"
        "**New Revision:** `{}`\n"
        "**Upstream Change:** {}\n\n"
        "_This PR was automatically generated by the OSS manifest automation._"
    ).format(description, layer_name, new_revision, source_pr_url)

    try:
        pr = repo.create_pull(title=title, body=pr_body, base=base_branch, head=head_branch)
        # Add auto-merge label last so label-triggered workflow sees final label set.
        for label in OSS_PR_LABELS:
            pr.add_to_labels(label)
        pr.add_to_labels(OSS_AUTO_MERGE_LABEL)
        print("OSS Auto PR Created and labeled: {}".format(pr.html_url))
        return pr
    except github.GithubException as e:
        print("Failed to create PR in {}: {}".format(full_repo_name, str(e)))
        return None


def update_revision_inplace(file_path, project_name, new_revision):
    """
    Update only revision attribute for matching <project> or <submanifest>,
    preserving formatting/attribute order.
    """
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    def replacer(match):
        return re.sub(
            r'(revision=["\'])([^"\']*?)(["\'])',
            lambda m: m.group(1) + new_revision + m.group(3),
            match.group(0),
            count=1
        )

    pattern = (
        r'<(?:project|submanifest)\s[^>]*?'
        r'(?:name|project)=["\']{}\s*["\'][^>]*?'
        r'(?:/>|>)'
    ).format(re.escape(project_name))

    new_content, count = re.subn(pattern, replacer, content, flags=re.DOTALL)
    if count == 0 or new_content == content:
        return False

    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(new_content)
    return True


def update_xml_files(manifest_repo_path, xml_files, updates):
    repo = Repo(manifest_repo_path)
    changes_made = False

    for xml_file in xml_files:
        xml_path = os.path.join(manifest_repo_path, xml_file)
        if not os.path.exists(xml_path):
            print("XML file not found, skipping: {}".format(xml_path))
            continue

        for project_name, new_revision in updates.items():
            if update_revision_inplace(xml_path, project_name, new_revision):
                print("Updated {}: project '{}' revision to '{}'".format(
                    xml_file, project_name, new_revision))
                changes_made = True
            else:
                print("No change needed for '{}' in {}".format(project_name, xml_file))

    if changes_made:
        repo.git.add(all=True)
    else:
        print("No changes were made to {}.".format(', '.join(xml_files)))
    return changes_made


def process_manifest_repo(github_token, repo_name, xml_files, workspace_dir, layer_name,
                          new_sha, feature_branch, base_branch, pr_title, pr_description,
                          updates, source_pr_url, source_pr_number, source_repo_name):
    full = "{}/{}".format(GITHUB_ORG, repo_name)
    try:
        print("Processing manifest repo: {}".format(full))

        manifest_path, repo = clone_manifest_repo(github_token, repo_name, workspace_dir)
        if not manifest_path or not repo:
            return {'repo': full, 'status': 'clone_failed'}

        g = Github(auth=Auth.Token(github_token))
        repo_obj = g.get_repo(full)

        # 1) If open PR already exists for this branch, stop here.
        existing_pr = find_open_pr_for_branch(repo_obj, feature_branch)
        if existing_pr:
            print("Open PR already exists for branch {} in {}: {}".format(
                feature_branch, full, existing_pr.html_url))
            return {
                'repo': full,
                'status': 'duplicate_skipped',
                'pr_url': existing_pr.html_url,
                'pr_number': existing_pr.number
            }

        # 2) Resume existing branch or create a new one.
        ok, branch_mode = ensure_branch_checked_out(repo, feature_branch, base_branch)
        if not ok:
            return {'repo': full, 'status': 'error', 'error': 'branch_prepare_failed'}

        # 3) Apply updates.
        changes_made = update_xml_files(manifest_path, xml_files, updates)

        # Recovery path: branch exists (resumed), no file diff now, but PR might be missing.
        if not changes_made and branch_mode == 'resumed':
            pr = create_oss_pull_request(
                github_token, repo_name, feature_branch, base_branch,
                pr_title, pr_description, layer_name, new_sha[:8], source_pr_url
            )
            if pr:
                post_pr_comment(source_repo_name, source_pr_number,
                                "OSS Auto PR created in {}: {}".format(full, pr.html_url),
                                github_token)
                return {
                    'repo': full,
                    'pr_url': pr.html_url,
                    'pr_number': pr.number,
                    'status': 'success'
                }

        if not changes_made:
            return {'repo': full, 'status': 'no_changes'}

        # 4) Commit/push and create PR.
        commit_and_push(
            manifest_repo_path=manifest_path,
            commit_message="[auto] Update {} to {} in {}".format(
                layer_name, new_sha[:8], ', '.join(xml_files))
        )

        pr = create_oss_pull_request(
            github_token, repo_name, feature_branch, base_branch,
            pr_title, pr_description, layer_name, new_sha[:8], source_pr_url
        )
        if not pr:
            return {'repo': full, 'status': 'error', 'error': 'PR creation failed'}

        post_pr_comment(source_repo_name, source_pr_number,
                        "OSS Auto PR created in {}: {}".format(full, pr.html_url),
                        github_token)
        return {
            'repo': full,
            'pr_url': pr.html_url,
            'pr_number': pr.number,
            'status': 'success'
        }

    except Exception as e:
        print("Error processing {}: {}".format(full, str(e)))
        return {'repo': full, 'status': 'error', 'error': str(e)}


def detect_manifest_type(layer_name, override):
    if override:
        return override
    return 'oss-common' if layer_name in occ.OSS_COMMON_LAYER_JOBS else 'oss'


def main():
    github_token = os.getenv('RDKCENTRAL_GITHUB_TOKEN') or os.getenv('GITHUB_TOKEN')
    repo_name    = os.getenv('SOURCE_REPOSITORY') or os.getenv('GITHUB_REPOSITORY')
    pr_number    = os.getenv('PR_NUMBER')
    base_branch  = os.getenv('BASE_BRANCH')
    workspace    = os.getenv('WORKSPACE')

    if not github_token:
        print("ERROR: RDKCENTRAL_GITHUB_TOKEN / GITHUB_TOKEN is not set.")
        sys.exit(1)
    if not workspace:
        print("Missing required WORKSPACE environment variable.")
        sys.exit(1)

    dispatch_layer    = os.getenv('DISPATCH_LAYER_NAME')
    dispatch_sha      = os.getenv('DISPATCH_NEW_SHA')
    dispatch_manifest = os.getenv('DISPATCH_MANIFEST_TYPE')

    manifest_workspace = os.path.join(workspace, 'oss_manifest_repos')
    os.makedirs(manifest_workspace, exist_ok=True)

    if dispatch_layer and dispatch_sha:
        print("Triggered via repository_dispatch (OSS CI)")
        print("Layer: {}/{} SHA: {}".format(GITHUB_ORG, dispatch_layer, dispatch_sha))

        layer_name       = dispatch_layer
        new_sha          = dispatch_sha
        manifest_type    = detect_manifest_type(layer_name, dispatch_manifest)
        target_base      = 'develop'
        pr_title         = "[auto] Update {} to {}".format(layer_name, new_sha[:8])
        pr_description   = (
            "Automated manifest update triggered by OSS CI.\n"
            "Layer  : {}/{}\n"
            "New SHA: {}"
        ).format(GITHUB_ORG, layer_name, new_sha)
        source_pr_url    = "https://github.com/{}/{}/commit/{}".format(
            GITHUB_ORG, layer_name, new_sha)
        updates          = {layer_name: new_sha}
        source_pr_number = None
        source_repo_name = "{}/{}".format(GITHUB_ORG, layer_name)

    else:
        print("Triggered via workflow_call (PR merged in layer repo)")

        if not all([repo_name, pr_number, base_branch]):
            print("Missing required environment variables (SOURCE_REPOSITORY, PR_NUMBER, BASE_BRANCH).")
            sys.exit(1)

        if repo_name.split('/')[0] != GITHUB_ORG:
            print("Source repository {} is not in {} org. Aborting.".format(repo_name, GITHUB_ORG))
            sys.exit(1)

        layer_name    = repo_name.split('/')[-1]
        manifest_type = detect_manifest_type(layer_name, os.getenv('MANIFEST_TYPE'))

        g = Github(auth=Auth.Token(github_token))
        source_repo = g.get_repo(repo_name)
        meta_pr = source_repo.get_pull(int(pr_number))

        prs, _, _ = fetch_merge_commits(GITHUB_ORG, repo_name, int(pr_number), github_token)
        if not prs:
            print("No merge commits found. Nothing to process.")
            sys.exit(0)

        new_sha       = prs[0]['sha']
        ticket_number = extract_ticket_number(meta_pr.title)
        pr_title      = "[auto] Update {} to {}".format(layer_name, new_sha[:8])
        if ticket_number != "NO-TICKET":
            pr_title = "{} : {}".format(ticket_number, pr_title)

        target_base      = 'develop' if manifest_type == 'oss-common' else base_branch
        updates          = {pr['repo'].split('/')[-1]: pr['sha'] for pr in prs}
        pr_description   = "{}{}".format(meta_pr.body or "", build_pr_list_description(prs))
        source_pr_url    = meta_pr.html_url
        source_pr_number = pr_number
        source_repo_name = repo_name

    feature_branch = "auto-{}-{}".format(layer_name, new_sha[:8])
    print("MANIFEST_TYPE: {} (layer: {})".format(manifest_type, layer_name))
    print("Updates to apply: {}".format(updates))

    if manifest_type == 'oss-common':
        target_repo, xml_files = OSS_COMMON_MANIFEST_REPO, [OSS_COMMON_MANIFEST_XML]
    else:
        target_repo, xml_files = OSS_MANIFEST_REPO, OSS_MANIFEST_XML_FILES

    print("\n--- Updating {}/{} ({}) ---".format(
        GITHUB_ORG, target_repo, ', '.join(xml_files)
    ))
    result = process_manifest_repo(
        github_token=github_token,
        repo_name=target_repo,
        xml_files=xml_files,
        workspace_dir=manifest_workspace,
        layer_name=layer_name,
        new_sha=new_sha,
        feature_branch=feature_branch,
        base_branch=target_base,
        pr_title=pr_title,
        pr_description=pr_description,
        updates=updates,
        source_pr_url=source_pr_url,
        source_pr_number=source_pr_number,
        source_repo_name=source_repo_name
    )

    # ── Summary
    print("\n" + "=" * 55)
    print("OSS MANIFEST AUTO PR SUMMARY")
    print("=" * 55)
    print("Repo   : {}".format(result.get('repo')))
    print("Status : {}".format(result.get('status')))
    if result.get('pr_url'):
        print("PR     : {}".format(result['pr_url']))
    if result.get('error'):
        print("Error  : {}".format(result['error']))

    try:
        shutil.rmtree(manifest_workspace)
    except Exception as e:
        print("Warning: Could not clean up workspace: {}".format(e))

    if result.get('status') in ('error', 'clone_failed'):
        sys.exit(1)


if __name__ == '__main__':
    main()