import os
import requests
import github
from github import Github
import xml.etree.ElementTree as ET
from git import Repo
import re
from git import GitCommandError
import sys
import time


#Fetch all merged PRs linked to a specific issue.
def fetch_merge_commits(owner, repo, pr_number, github_token):
 
    url = 'https://api.github.com/graphql'
    repo_without_org = repo.split('/')[-1]
    headers = {'Authorization': 'Bearer {}'.format(github_token)}
    query = """
    query($repoOwner: String!, $repoName: String!, $prNumber: Int!) {
          repository(owner: $repoOwner, name: $repoName) {
            nameWithOwner
            pullRequest(number: $prNumber) {
              merged
              mergeCommit {
                oid
              }
              repository {
                  nameWithOwner
              }
              timelineItems(last: 100, itemTypes: [CONNECTED_EVENT]) {
                nodes {
                  ... on ConnectedEvent {
                    subject {
                      __typename
                      ... on Issue {
                        number
                        title
                        repository {
                          nameWithOwner
                        }
                        timelineItems(last: 100, itemTypes: [CONNECTED_EVENT]) {
                          nodes {
                            ... on ConnectedEvent {
                              subject {
                                __typename
                                ... on PullRequest {
                                  number
                                  merged
                                  mergeCommit {
                                    oid
                                  }
                                  repository {
                                      nameWithOwner
                                  }
                                }
                              }
                            }
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
          }
        }
    """
    variables = {
        'repoOwner': owner,
        'repoName': repo_without_org,
        'prNumber': pr_number
    }
    response = requests.post(url, json={'query': query, 'variables': variables}, headers=headers)
    result = response.json()
    print(result)

    prs = []
    main_pr_merge_commit = None
    issue_number = None
    issue_repo_name = None

    if response.status_code == 200:
        data = result['data']['repository']['pullRequest']
        #print(data)
        # Fetch the main PR merge commit
        if data.get('merged') and data.get('mergeCommit'):
                main_pr_merge_commit = {
                    'repo': data['repository']['nameWithOwner'],
                    'sha': data['mergeCommit']['oid']
                }
        
        # Find the linked issue number
        for node in data['timelineItems']['nodes']:
            subject = node.get('subject', {})
            if subject.get('__typename') == 'Issue':
                issue_number = subject['number']
                issue_repo_name = subject['repository']['nameWithOwner']
                break

        # Extract all linked PRs' merge commits from the connected issue
        for node in data['timelineItems']['nodes']:
            subject = node.get('subject', {})
            if subject.get('__typename') == 'Issue':
                issue_number = subject['number']
                issue_nodes = subject['timelineItems']['nodes']
                for issue_node in issue_nodes:
                    issue_pr = issue_node['subject']
                    if issue_pr.get('__typename') == 'PullRequest' and issue_pr.get('merged') and issue_pr['mergeCommit']:
                        prs.append({
                            'repo': issue_pr['repository']['nameWithOwner'],
                            'sha': issue_pr['mergeCommit']['oid']
                        })

    else:
        print("Failed to fetch data:", result.get('errors'))

    # If no linked PRs are found, return only the input PR's merge commit
    if not prs and main_pr_merge_commit:
        prs.append(main_pr_merge_commit)
    
    if issue_number :
        return prs, issue_repo_name, issue_number
    else:
        return prs, None, None

#Extract the ticket number from the PR title
def extract_ticket_number(pr_title):

    ticket_pattern = r"[A-Z]+-[0-9]+"
    match = re.search(ticket_pattern, pr_title)
    return match.group(0) if match else "NO-TICKET"

# function to write the xml file
def write_xml(element, file_path):
    tree = ET.ElementTree(element)
    tree.write(file_path, encoding='utf-8', xml_declaration=True)

# function to update xml files
def update_xml_files(manifest_repo_path, updates):

  repo = Repo(manifest_repo_path)

  # Recursively search for entservices-inputoutput.bb in all subdirectories
  bb_file = None
  for root, dirs, files in os.walk(manifest_repo_path):
      for f in files:
          if f == 'entservices-inputoutput.bb':
              bb_file = os.path.join(root, f)
              break
      if bb_file:
          break

  if not bb_file:
      print("No entservices-inputoutput.bb recipe file found.")
      return False

  # Get the new SHA (assume only one update, as in your usage)
  new_srcrev = list(updates.values())[0]
  changed = False
  with open(bb_file, 'r') as f:
      lines = f.readlines()



  with open(bb_file, 'w') as f:
      for line in lines:
          if line.strip().startswith('SRCREV ='):
              old_line = line.strip()
              f.write(f'SRCREV = "{new_srcrev}"\n')
              print(f"Updated SRCREV in {bb_file}: {old_line} -> SRCREV = \"{new_srcrev}\"")
              changed = True
          else:
              f.write(line)

  if changed:
      repo.git.add(bb_file)
  else:
      print("No changes were made to SRCREV in the recipe file.")

  return changed
def update_bb_and_pkgrev(manifest_repo_path, generic_support_path, updates):
    """
    For each component, update the correct .bb file's SRCREV and the correct generic-pkgrev.inc PV field with the tag.
    updates: list of dicts with keys: repo, sha, tag
    """
    repo = Repo(manifest_repo_path)
    support_repo = None
    support_repo_path = generic_support_path
    if os.path.isdir(support_repo_path):
        try:
            support_repo = Repo(support_repo_path)
        except Exception as e:
            print(f"[DEBUG] Could not open support repo at {support_repo_path}: {e}")
    changed = False
    support_changed = False
    pkgrev_file = None
    # Find generic-pkgrev.inc once
    for root, dirs, files in os.walk(generic_support_path):
        for f in files:
            if f == "generic-pkgrev.inc":
                pkgrev_file = os.path.join(root, f)
                break
        if pkgrev_file:
            break
    if not pkgrev_file:
        print(f"[WARNING] generic-pkgrev.inc file not found in {generic_support_path}")
    for update in updates:
        repo_name = update['repo']
        sha = update['sha']
        tag = update.get('tag')
        print(f"[DEBUG] Processing update: repo={repo_name}, sha={sha}, tag={tag}")
        bb_file = None
        comp = None
        pkgrev_pv_field = None
        if repo_name.startswith('rdkcentral/entservices-'):
            comp = repo_name.split('/')[-1]
            # Find .bb file
            for root, dirs, files in os.walk(manifest_repo_path):
                for f in files:
                    if f == f"{comp}.bb":
                        bb_file = os.path.join(root, f)
                        break
                if bb_file:
                    break
            # Set PV field for generic-pkgrev.inc
            pkgrev_pv_field = f'PV:pn-{comp}'
        # Informational: bb_file and pkgrev_file
        # Update .bb file SRCREV and PV in meta layer
        if bb_file and os.path.exists(bb_file):
            # Opening bb_file: {bb_file}
            with open(bb_file, 'r', newline='') as f:
                old_lines = f.readlines()
            file_changed = False
            tag_to_use = tag
            new_lines = []
            for idx, line in enumerate(old_lines):
                line_stripped = line.strip()
                # bb_file line {idx}: {line_stripped}
                # Match SRCREV, SRCREV ?=, or SRCREV_<component> = (component always lower case)
                if (
                    line_stripped.startswith('SRCREV =') or
                    line_stripped.startswith('SRCREV ?=') or
                    (line_stripped.startswith(f'SRCREV_{comp} =') if comp else False)
                ):
                    # Updating SRCREV in {bb_file} to {sha}
                    # Preserve the assignment type (e.g., =, ?=)
                    if 'SRCREV ?=' in line_stripped:
                        new_lines.append(f'SRCREV ?= "{sha}"\n')
                    elif f'SRCREV_{comp} =' in line_stripped:
                        new_lines.append(f'SRCREV_{comp} = "{sha}"\n')
                    else:
                        new_lines.append(f'SRCREV = "{sha}"\n')
                    file_changed = True
                elif line_stripped.startswith('PV ?='):
                    # Updating PV in {bb_file} to {tag_to_use}
                    new_lines.append(f'PV ?= "{tag_to_use}"\n')
                    file_changed = True
                else:
                    new_lines.append(line)
            # Print diff for debugging
            # Diff and content change info removed
            # Write only if changed
            if old_lines != new_lines:
                with open(bb_file, 'w', newline='\n') as f:
                    f.writelines(new_lines)
                repo.git.add(bb_file)
                changed = True
                print(f"Updated {bb_file}")
            else:
                print(f"No change needed for {bb_file}")
        else:
            print(f"[DEBUG] .bb file does not exist: {bb_file}")

        # Update PV:pn-<comp_name> in generic-pkgrev.inc for all components
        if pkgrev_file and os.path.exists(pkgrev_file) and support_repo and pkgrev_pv_field:
            print(f"[DEBUG] Attempting to update PV field {pkgrev_pv_field} in {pkgrev_file}")
            tag_to_use = tag
            with open(pkgrev_file, 'r', newline='') as f:
                old_lines = f.readlines()
            new_lines = []
            file_changed = False
            found_pv = False
            for idx, line in enumerate(old_lines):
                if line.strip().startswith(f'{pkgrev_pv_field} ='):
                    print("Updating existing PV field")
                    new_lines.append(f'{pkgrev_pv_field} = "{tag_to_use}"\n')
                    file_changed = True
                    found_pv = True
                else:
                    new_lines.append(line)
            if not found_pv:
                print(f"PV field {pkgrev_pv_field} not found in {pkgrev_file}, appending new line.")
                new_lines.append(f'{pkgrev_pv_field} = "{tag_to_use}"\n')
                file_changed = True
            if old_lines != new_lines:
                with open(pkgrev_file, 'w', newline='\n') as f:
                    f.writelines(new_lines)
                support_repo.git.add(pkgrev_file)
                support_changed = True
                print(f"Updated {pkgrev_file}")
            else:
                print(f"No change needed for {pkgrev_file}")
        elif pkgrev_file:
            print(f"[WARNING] pkgrev_file exists but support_repo not found: {pkgrev_file}")
        else:
            print(f"[WARNING] generic-pkgrev.inc file not found, skipping support layer update.")
    print(f"update_bb_and_pkgrev completed")
    return changed, support_changed, support_repo
#Build the PR list description
def build_pr_list_description(prs):

    pr_list = "\n\nList of PRs and Repositories Involved:\n"
    for pr in prs:
        repo_name = pr['repo']
        sha = pr['sha']
        pr_list += "- Repository: {}, Merge Commit SHA: {}\n".format(repo_name, sha)
    return pr_list

#Commit the changes to the manifest files and push to the feature branch
def commit_and_push(manifest_repo_path, commit_message):
    repo = Repo(manifest_repo_path)
    if repo.is_dirty():
        # Ensure git user.name and user.email are set
        config_writer = repo.config_writer()
        try:
            user_name = config_writer.get_value('user', 'name', None)
            user_email = config_writer.get_value('user', 'email', None)
        except Exception:
            user_name = None
            user_email = None
        # Set from environment only, do not hardcode defaults
        if not user_name:
            committer_name = os.environ.get('GIT_COMMITTER_NAME')
            if committer_name:
                config_writer.set_value('user', 'name', committer_name)
        if not user_email:
            committer_email = os.environ.get('GIT_COMMITTER_EMAIL')
            if committer_email:
                config_writer.set_value('user', 'email', committer_email)
        config_writer.release()
        repo.git.commit('-m', commit_message)
        repo.git.push('origin', repo.active_branch.name)
    else:
        print("No changes to commit.")


#Create a new PR for the updated manifest files
def create_pull_request(github_token, repo_name, head_branch, base_branch, title, description):
    g = Github(github_token)
    repo = g.get_repo(repo_name)

    # Check if a PR already exists for this branch
    pulls = repo.get_pulls(state='open', head=f"{repo.owner.login}:{head_branch}")
    for pr in pulls:
        print(f"[DEBUG] Existing PR found for branch {head_branch}: {pr.html_url}")
        return pr
    try:
        # Only create the PR, do NOT merge it. Manual review/merge is required.
        pr = repo.create_pull(title=title, body=description, base=base_branch, head=head_branch)
        print("PR Created:", pr.html_url)
        return pr
    except github.GithubException as e:
        print("Failed to create PR:", str(e))
        return None

# Create a summary issue linking meta*video and meta*support auto PRs
def create_summary_issue(github_token, repo_name, issue_title, issue_body):
    g = Github(github_token)
    repo = g.get_repo(repo_name)
    # Check for existing open issue with the same title
    for issue in repo.get_issues(state='open'):
        if issue.title == issue_title:
            print(f"Summary issue already exists: {issue.html_url}")
            return issue
    # If not found, create new
    issue = repo.create_issue(title=issue_title, body=issue_body)
    print(f"Issue created: {issue.html_url}")
    return issue

#Ensure that the label exists in the repository
def ensure_label_exists(repo, label_name, color='FFFFFF'):
    labels = repo.get_labels()
    label_list = {label.name: label for label in labels}
    if label_name not in label_list:
      repo.create_label(name=label_name, color=color)
      print("Label '{}' created.".format(label_name))
    else:
      print("Label '{}' already exists.".format(label_name))

#Create or checkout the branch in the local manifest repository
def create_or_checkout_branch(repo, branch_name, base_branch):
    
    try:
        # Fetch all branches from the remote
        repo.git.fetch('origin')

        # Ensure git user.name and user.email are set for this repo
        config_writer = repo.config_writer()
        try:
            user_name = config_writer.get_value('user', 'name', None)
            user_email = config_writer.get_value('user', 'email', None)
        except Exception:
            user_name = None
            user_email = None
        if not user_name:
            config_writer.set_value('user', 'name', os.environ.get('GIT_COMMITTER_NAME', 'github-actions[bot]'))
        if not user_email:
            config_writer.set_value('user', 'email', os.environ.get('GIT_COMMITTER_EMAIL', 'github-actions[bot]@users.noreply.github.com'))
        config_writer.release()

        # Check if the branch exists in the remote repository
        remote_branches = [b.strip() for b in repo.git.branch('-r').split('\n')]
        remote_branch_full = f'origin/{branch_name}'
        local_branches = [b.name for b in repo.branches]
        if remote_branch_full in remote_branches:
            print(f"Branch {branch_name} already exists remotely. Checking out and updating it.")
            # Commit any outstanding changes before switching branches
            if repo.is_dirty():
                try:
                    repo.git.add('--all')
                    repo.git.commit('-m', 'WIP: commit outstanding changes before branch switch')
                except Exception as e:
                    print(f"[DEBUG] Could not commit outstanding changes: {e}")
            # If we are not already on the branch, check it out
            current_branch = repo.active_branch.name
            if current_branch != branch_name:
                if branch_name not in local_branches:
                    repo.git.checkout('-b', branch_name, remote_branch_full)
                else:
                    repo.git.checkout(branch_name)
            # Always pull latest changes from remote
            repo.git.pull('origin', branch_name)
        else:
            # Switch to 'base_branch' and pull the latest changes to ensure local repo is up-to-date
            repo.git.checkout(base_branch)
            repo.git.pull('origin', base_branch)

            # Create and check out the new branch locally
            repo.git.checkout('-b', branch_name)

            # Push the newly created branch to the remote
            repo.git.push('origin', branch_name)

    except GitCommandError as e:
        print(f"Error checking out branch: {str(e)}")
        sys.exit(1)

def get_tag_for_sha(github_token, repo_full_name, sha):
    """
    Return the tag name for a given repo and commit SHA, or None if not found.
    """
    from packaging.version import Version, InvalidVersion
    g = Github(github_token)
    repo = g.get_repo(repo_full_name)
    tags = [tag for tag in repo.get_tags() if re.match(r'^\d+\.\d+\.\d+$', tag.name)]
    # Try exact match first
    for tag in tags:
        if tag.commit.sha.startswith(sha):
            return tag.name
    # Find all tags whose commit is a descendant of the given sha (i.e., tag contains the commit)
    tags_with_commit = []
    try:
        for tag in tags:
            tag_commit = repo.get_commit(tag.commit.sha)
            comparison = repo.compare(sha, tag_commit.sha)
            if comparison.status == 'ahead':
                tags_with_commit.append(tag)
    except Exception as e:
        print(f"[DEBUG] Error checking if tag contains commit: {e}")

    # Sort tags_with_commit by semantic version (lowest first)
    def version_key(tag):
        try:
            return Version(tag.name)
        except InvalidVersion:
            return Version('0.0.0')
    tags_with_commit.sort(key=version_key)

    # Now, for each tag (in ascending order), check if the previous (lower) tag does NOT contain the commit
    for i, tag in enumerate(tags_with_commit):
        if i == 0:
            # If this is the lowest tag that contains the commit, always return it
            return tag.name
        prev_tag = tags_with_commit[i-1]
        # Check if previous tag contains the commit
        try:
            prev_tag_commit = repo.get_commit(prev_tag.commit.sha)
            comparison = repo.compare(sha, prev_tag_commit.sha)
            if comparison.status != 'ahead':
                # Previous tag does NOT contain the commit, so this is the first tag that does
                return tag.name
        except Exception as e:
            print(f"[DEBUG] Error comparing with previous tag: {e}")
            return tag.name
    # Fallback: if all lower tags also contain the commit, return the lowest
    if tags_with_commit:
        return tags_with_commit[0].name
    return None

def main():
    github_token = os.getenv('GITHUB_TOKEN')
    meta_repo_path = os.getenv('META_REPO_PATH')
    generic_support_path = os.getenv('GENERIC_SUPPORT_PATH')  # new env var for meta-middleware-generic-support
    pr_number = os.getenv('PR_NUMBER')
    meta_repo_name = os.getenv('META_REPO_NAME')
    repo_name = os.getenv('GITHUB_REPOSITORY')
    repo_owner = os.getenv('GITHUB_ORG')
    base_branch = os.getenv('BASE_BRANCH')

    # Check required environment variables
    if not generic_support_path:
        print("ERROR: GENERIC_SUPPORT_PATH environment variable is not set.")
        sys.exit(1)
    if not meta_repo_path:
        print("ERROR: META_REPO_PATH environment variable is not set.")
        sys.exit(1)
    if not github_token:
        print("ERROR: GITHUB_TOKEN environment variable is not set.")
        sys.exit(1)
    if not pr_number:
        print("ERROR: PR_NUMBER environment variable is not set.")
        sys.exit(1)
    if not meta_repo_name:
        print("ERROR: META_REPO_NAME environment variable is not set.")
        sys.exit(1)
    if not repo_name:
        print("ERROR: GITHUB_REPOSITORY environment variable is not set.")
        sys.exit(1)
    if not repo_owner:
        print("ERROR: GITHUB_ORG environment variable is not set.")
        sys.exit(1)
    if not base_branch:
        print("ERROR: BASE_BRANCH environment variable is not set.")
        sys.exit(1)

    g = Github(github_token)
    repo = g.get_repo(repo_name)
    meta_pr = repo.get_pull(int(pr_number))

    # Extract ticket number
    ticket_number = extract_ticket_number(meta_pr.title)

    prs, issue_repo_name, issue_number = fetch_merge_commits(repo_owner, repo_name, int(pr_number), github_token)
    # If PR is linked to an issue, check if all PRs in that issue are merged to the target branch
    if issue_number:
        print(f"[DEBUG] PR is linked to issue #{issue_number}. Checking all linked PRs...")
        all_merged = True
        g = Github(github_token)
        for pr_info in prs:
            repo_full = pr_info['repo']
            sha = pr_info['sha']
            repo_obj = g.get_repo(repo_full)
            # Find the PR for this SHA
            found = False
            for pr in repo_obj.get_pulls(state='closed'):
                if pr.merge_commit_sha and pr.merge_commit_sha.startswith(sha):
                    # Check if merged to the correct base branch
                    if pr.merged and pr.base.ref == base_branch:
                        found = True
                        break
            if not found:
                all_merged = False
                print(f"[DEBUG] Not all PRs are merged to {base_branch}. Waiting...")
                break
        if not all_merged:
            print(f"[DEBUG] Exiting: Not all PRs in issue #{issue_number} are merged to {base_branch}.")
            sys.exit(0)
    # Continue as before: aggregate all PRs for update
    print(f"[DEBUG] PRs to process: {prs}")
    updates = []
    for pr in prs:
        tag = get_tag_for_sha(github_token, pr['repo'], pr['sha'])
        if not tag:
            print(f"[WARNING] No tag found that contains commit {pr['sha']} for repo {pr['repo']}. Setting tag as None.")
            tag = None
        print(f"[DEBUG] Tag for {pr['repo']} at {pr['sha']}: {tag}")
        updates.append({'repo': pr['repo'], 'sha': pr['sha'], 'tag': tag})

    print("Updates to be pushed to topic branch: {}".format(updates))

    meta_pr_obj = None
    support_pr_obj = None
    meta_pr_url = None
    support_pr_url = None
    summary_issue_url = None
    comp_name = updates[0]['repo'].split('/')[-1] if updates else "unknown"
    # --- Refactored support layer PV update logic ---
    # Branch setup
    if issue_number:
        feature_branch = f"topic/auto-{ticket_number.lower()}-issue-{issue_number}"
        support_branch = f"topic/auto-support-{ticket_number.lower()}-issue-{issue_number}"
    else:
        feature_branch = f"topic/auto-{ticket_number.lower()}-pr-{pr_number}"
        support_branch = f"topic/auto-support-{ticket_number.lower()}-pr-{pr_number}"

    meta_pr_title = f"[Auto] Update meta layer for {ticket_number}" + (f" (PR {pr_number})" if not issue_number else "")
    meta_pr_description = build_pr_list_description(updates)
    create_or_checkout_branch(Repo(meta_repo_path), feature_branch, base_branch)

    # --- Update meta layer as before ---
    changes_made, _, _ = update_bb_and_pkgrev(meta_repo_path, generic_support_path, updates)
    if changes_made:
        commit_and_push(
            meta_repo_path,
            "Update meta and pkgrev for {}".format(', '.join([f"{u['repo'].split('/')[-1]}:{u['sha'][:7]}:{u['tag']}" for u in updates]))
        )
        meta_pr_obj = create_pull_request(
            github_token,
            meta_repo_name,
            feature_branch,
            base_branch,
            meta_pr_title,
            meta_pr_description
        )
        if meta_pr_obj:
            meta_pr_url = meta_pr_obj.html_url
            print(f"[INFO] Meta layer PR created: {meta_pr_url}")
    else:
        # Try to find existing PR for this branch
        g = Github(github_token)
        repo = g.get_repo(meta_repo_name)
        pulls = repo.get_pulls(state='open', head=f"{repo.owner.login}:{feature_branch}")
        for pr in pulls:
            meta_pr_obj = pr
            meta_pr_url = pr.html_url
            print(f"[INFO] Existing Meta layer PR: {meta_pr_url}")
            break
        if not meta_pr_url:
            print("[DEBUG] No changes detected, PR will not be created for meta layer.")

    # --- Support layer PV update logic ---
    support_repo = None
    if os.path.isdir(generic_support_path):
        try:
            support_repo = Repo(generic_support_path)
        except Exception as e:
            print(f"[DEBUG] Could not open support repo at {generic_support_path}: {e}")

    pkgrev_file = None
    # Find generic-pkgrev.inc once
    for root, dirs, files in os.walk(generic_support_path):
        for f in files:
            if f == "generic-pkgrev.inc":
                pkgrev_file = os.path.join(root, f)
                break
        if pkgrev_file:
            break
    if not pkgrev_file:
        print(f"[WARNING] generic-pkgrev.inc file not found in {generic_support_path}")
    else:
        # Prepare PV updates
        pv_updates = []
        if issue_number:
            # Update PV for all components in updates
            for update in updates:
                comp = update['repo'].split('/')[-1]
                pv_field = f'PV:pn-{comp}'
                pv_updates.append((pv_field, update['tag']))
        else:
            # Update PV for only the single PR's component
            comp = updates[0]['repo'].split('/')[-1]
            pv_field = f'PV:pn-{comp}'
            pv_updates.append((pv_field, updates[0]['tag']))


        # Read all lines from pkgrev_file
        with open(pkgrev_file, 'r', newline='') as f:
            old_lines = f.readlines()

        new_lines = []
        changed = False
        # Track which PV fields are updated
        updated_fields = {pv_field: False for pv_field, _ in pv_updates}
        # Only keep the first occurrence of each PV field, and replace it
        for line in old_lines:
            line_stripped = line.strip()
            replaced = False
            for pv_field, tag in pv_updates:
                if line_stripped.startswith(f'{pv_field} ='):
                    if not updated_fields[pv_field]:
                        new_lines.append(f'{pv_field} = "{tag}"\n')
                        changed = True
                        updated_fields[pv_field] = True
                    # If already updated, skip this duplicate line
                    replaced = True
                    break
            if not replaced:
                new_lines.append(line)
        # Add any missing PV fields
        for pv_field, tag in pv_updates:
            if not updated_fields[pv_field]:
                print(f"[DEBUG] Adding new PV field: {pv_field} = {tag}")
                new_lines.append(f'{pv_field} = "{tag}"\n')
                changed = True

        # If any PV field changed, update file and commit/push
    support_pr_obj = None
    if changed and support_repo:
        create_or_checkout_branch(support_repo, support_branch, base_branch)
        with open(pkgrev_file, 'w', newline='\n') as f:
            f.writelines(new_lines)
        support_repo.git.add(pkgrev_file)
        commit_and_push(
            generic_support_path,
            "Update support layer PV fields for {}".format(', '.join([f"{pv}" for pv, _ in pv_updates]))
        )
        support_pr_obj = create_pull_request(
            github_token,
            'rdkcentral/meta-middleware-generic-support',
            support_branch,
            base_branch,
            f"[Auto] Update support layer for {ticket_number}" + (f" (PR {pr_number})" if not issue_number else ""),
            build_pr_list_description(updates)
        )
        if support_pr_obj:
            support_pr_url = support_pr_obj.html_url
            print(f"[INFO] Support layer PR created: {support_pr_url}")
    elif not support_repo:
        print("[DEBUG] No support_repo found for PR creation.")
    else:
        # Try to find existing PR for this branch
        g = Github(github_token)
        repo = g.get_repo('rdkcentral/meta-middleware-generic-support')
        pulls = repo.get_pulls(state='open', head=f"{repo.owner.login}:{support_branch}")
        for pr in pulls:
            support_pr_obj = pr
            support_pr_url = pr.html_url
            print(f"[INFO] Existing Support layer PR: {support_pr_url}")
            break
        if not support_pr_url:
            print("[DEBUG] No PV changes needed for support layer.")

    # --- Always print summary issue link if both PRs exist, or if summary issue exists ---
    if meta_pr_obj and support_pr_obj:
        pr_links = []
        pr_links.append(f"- [Meta Video Auto PR]({meta_pr_obj.html_url})")
        pr_links.append(f"- [Meta Support Auto PR]({support_pr_obj.html_url})")
        # Compose a unique summary issue title for the topic branch/issue
        if issue_number:
            comp_names = ', '.join([u['repo'].split('/')[-1] for u in updates])
            issue_title = f"[Auto] {ticket_number} - Meta Layer Updates for Issue {issue_number} ({comp_names})"
        else:
            comp_names = updates[0]['repo'].split('/')[-1] if updates else "unknown"
            issue_title = f"[Auto] {ticket_number} - Meta Layer Updates for PR {pr_number} ({comp_names})"
        issue_body = (
            f"## Automated Update Summary for {ticket_number} " + (f"(Issue {issue_number})" if issue_number else f"(PR {pr_number})") + "\n\n"
            f"### PRs Created:\n" +
            "\n".join(pr_links) +
            "\n\n### Details:\n" +
            build_pr_list_description(updates)
        )
        # Only create the summary issue if it does not already exist for this topic branch/issue
        g = Github(github_token)
        repo = g.get_repo(meta_repo_name)
        existing_issue = None
        for issue in repo.get_issues(state='open'):
            if issue_number:
                if f"{ticket_number}" in issue.title and f"Meta Layer Updates for Issue {issue_number}" in issue.title:
                    print(f"[INFO] Summary issue already exists: {issue.html_url}")
                    existing_issue = issue
                    summary_issue_url = issue.html_url
                    break
            else:
                if f"{ticket_number}" in issue.title and f"Meta Layer Updates for PR {pr_number}" in issue.title:
                    print(f"[INFO] Summary issue already exists: {issue.html_url}")
                    existing_issue = issue
                    summary_issue_url = issue.html_url
                    break
        if not existing_issue:
            created_issue = create_summary_issue(github_token, meta_repo_name, issue_title, issue_body)
            if created_issue:
                summary_issue_url = created_issue.html_url
                print(f"[INFO] Summary issue created: {summary_issue_url}")
    else:
        # Try to find existing summary issue and print its link
        g = Github(github_token)
        repo = g.get_repo(meta_repo_name)
        for issue in repo.get_issues(state='open'):
            if issue_number:
                if f"{ticket_number}" in issue.title and f"Meta Layer Updates for Issue {issue_number}" in issue.title:
                    summary_issue_url = issue.html_url
                    print(f"[INFO] Summary issue (existing): {summary_issue_url}")
                    break
            else:
                if f"{ticket_number}" in issue.title and f"Meta Layer Updates for PR {pr_number}" in issue.title:
                    summary_issue_url = issue.html_url
                    print(f"[INFO] Summary issue (existing): {summary_issue_url}")
                    break

    # --- Print all links at the end for traceability ---
    print("\n==== Traceability Links ====")
    if meta_pr_url:
        print(f"Meta PR: {meta_pr_url}")
    if support_pr_url:
        print(f"Support PR: {support_pr_url}")
    if summary_issue_url:
        print(f"Summary Issue: {summary_issue_url}")
if __name__ == '__main__':
    main()
