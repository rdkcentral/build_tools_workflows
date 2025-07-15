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
    for update in updates:
        repo_name = update['repo']
        sha = update['sha']
        tag = update.get('tag')
        print(f"[DEBUG] Processing update: repo={repo_name}, sha={sha}, tag={tag}")
        # Determine .bb paths (meta and support layer)
        bb_file = None
        support_bb_file = None
        pkgrev_file = None
        pkgrev_pv_field = None
        if repo_name.startswith('rdkcentral/entservices-'):
            comp = repo_name.split('/')[-1]
            # Determine .bb file location for entservices-apis and other entservices-* components
            if comp == 'entservices-apis':
                bb_file = os.path.join(manifest_repo_path, 'recipes-extended', 'wpe-framework', 'entservices-apis.bb')
            else:
                bb_file = os.path.join(manifest_repo_path, 'recipes-extended', 'entservices', f'{comp}.bb')
            # For support layer PR (if support .bb files exist, keep logic, else skip)
            support_entservices_dir = os.path.join(generic_support_path, 'recipes-extended', 'entservices')
            support_bb_file = os.path.join(support_entservices_dir, f'{comp}.bb')
            pkgrev_file = os.path.join(generic_support_path, 'conf', 'include', 'generic-pkgrev.inc')
            pkgrev_pv_field = f'PV:pn-{comp}'
        else:
            print(f"[DEBUG] Skipping repo {repo_name} (not handled)")
            continue
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


        # Update PV in support layer for all entservices-* components
        if pkgrev_file and os.path.exists(pkgrev_file) and support_repo:
            tag_to_use = tag
            with open(pkgrev_file, 'r', newline='') as f:
                old_lines = f.readlines()
            file_changed = False
            found_pv = False
            previous_value = None
            for line in old_lines:
                if line.strip().startswith(f'{pkgrev_pv_field} ='):
                    previous_value = line.strip().split('=')[1].strip().strip('"')
                    found_pv = True
                    break
            if found_pv:
                if previous_value != str(tag_to_use):
                    new_lines = []
                    for line in old_lines:
                        if line.strip().startswith(f'{pkgrev_pv_field} ='):
                            new_lines.append(f'{pkgrev_pv_field} = "{tag_to_use}"\n')
                        else:
                            new_lines.append(line)
                    with open(pkgrev_file, 'w', newline='\n') as f:
                        f.writelines(new_lines)
                    support_repo.git.add(pkgrev_file)
                    support_changed = True
                    print(f"Updated {pkgrev_file}. Previous value: '{previous_value}', new value: '{tag_to_use}'")
                else:
                    print(f"No change needed for {pkgrev_file}. Reason: PV field for {pkgrev_pv_field} already matches the desired value. Previous value: '{previous_value}', new value: '{tag_to_use}'")
            else:
                print(f"PV field {pkgrev_pv_field} not found in {pkgrev_file}. No update performed.")
        elif pkgrev_file:
            print(f"pkgrev_file does not exist or support_repo not found: {pkgrev_file}")
        # --- DEBUG: Print comp, pkgrev_pv_field, and tag for every update attempt ---
        # Informational: update_bb_and_pkgrev status
        tag_to_use = tag
        if pkgrev_file and os.path.exists(pkgrev_file) and support_repo:
            # Opening pkgrev_file: {pkgrev_file}
            with open(pkgrev_file, 'r', newline='') as f:
                old_lines = f.readlines()
            file_changed = False
            found_pv = False
            new_lines = []
            for idx, line in enumerate(old_lines):
                # pkgrev_file line {idx}: {line.strip()}
                if line.strip().startswith(f'{pkgrev_pv_field} ='):
                    # Updating PV in {pkgrev_file} to {tag_to_use} (line {idx})
                    new_lines.append(f'{pkgrev_pv_field} = "{tag_to_use}"\n')
                    file_changed = True
                    found_pv = True
                else:
                    new_lines.append(line)
            if not found_pv:
                print(f"PV field {pkgrev_pv_field} not found in {pkgrev_file}, appending new line.")
                new_lines.append(f'{pkgrev_pv_field} = "{tag_to_use}"\n')
                file_changed = True
            # Print diff for debugging
            # Diff and content change info removed
            if old_lines != new_lines:
                with open(pkgrev_file, 'w', newline='\n') as f:
                    f.writelines(new_lines)
                support_repo.git.add(pkgrev_file)
                support_changed = True
                print(f"Updated {pkgrev_file}")
            else:
                print(f"No change needed for {pkgrev_file}")
        elif pkgrev_file:
            print(f"pkgrev_file does not exist or support_repo not found: {pkgrev_file}")
    print(f"update_bb_and_pkgrev completed")
    #print the changed status and diff 
    if not support_changed:
        print("[DEBUG] No changes made to support layer.")
    else:
        print(f"[DEBUG] Changes made to support layer in the branch: {support_repo.active_branch.name}")
        #print the new lines in the support layer
        for line in new_lines:
            print(f"[DEBUG] {line.strip()}")
        # --- DEBUG: Print the diff of support layer changes for debugging ---
        if old_lines != new_lines:
            print("[DEBUG] Support layer changes detected:")
            for old_line, new_line in zip(old_lines, new_lines):
                if old_line != new_line:
                    print(f"[DEBUG] - {old_line.strip()} -> {new_line.strip()}")
        # Print the diff of support layer changes for debugging
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
        existing_branches = repo.git.branch('-r')
        if f'origin/{branch_name}' in existing_branches:
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
                # If branch does not exist locally, create tracking branch
                local_branches = [b.name for b in repo.branches]
                if branch_name not in local_branches:
                    repo.git.checkout('-b', branch_name, f'origin/{branch_name}')
                else:
                    repo.git.checkout(branch_name)
            # Pull latest changes from remote
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
        print(f"[DEBUG] Tag for {pr['repo']} at {pr['sha']}: {tag}")
        if not tag:
            print(f"[WARNING] No tag found that contains commit {pr['sha']} for repo {pr['repo']}. Setting tag as None.")
            tag = None
        updates.append({'repo': pr['repo'], 'sha': pr['sha'], 'tag': tag})

    print("Updates to be pushed to topic branch: {}".format(updates))

    meta_pr_obj = None
    support_pr_obj = None
    comp_name = updates[0]['repo'].split('/')[-1] if updates else "unknown"
    auto_pr_links = []
    # --- Refactored PV update logic for support layer ---
    if issue_number:
        # Issue-linked PR: update PV for only the merged component in the shared topic branch
        feature_branch = f"topic/auto-{ticket_number.lower()}-issue-{issue_number}"
        meta_pr_title = f"[Auto] Update meta layer for {ticket_number}"
        meta_pr_description = build_pr_list_description(updates)
        create_or_checkout_branch(Repo(meta_repo_path), feature_branch, base_branch)
        support_branch = f"topic/auto-support-{ticket_number.lower()}-issue-{issue_number}"
        support_repo = None
        if os.path.isdir(generic_support_path):
            try:
                support_repo = Repo(generic_support_path)
                create_or_checkout_branch(support_repo, support_branch, base_branch)
                # Only update PV field for the current merged component in generic-pkgrev.inc
                pkgrev_file = os.path.join(generic_support_path, 'conf', 'include', 'generic-pkgrev.inc')
                if os.path.exists(pkgrev_file):
                    comp = updates[0]['repo'].split('/')[-1]
                    pv_field = f'PV:pn-{comp}'
                    with open(pkgrev_file, 'r', newline='') as f:
                        old_lines = f.readlines()
                    new_lines = []
                    found_pv = False
                    for line in old_lines:
                        if line.strip().startswith(f'{pv_field} ='):
                            new_lines.append(f'{pv_field} = "{updates[0]['tag']}"\n')
                            found_pv = True
                        else:
                            new_lines.append(line)
                    if not found_pv:
                        new_lines.append(f'{pv_field} = "{updates[0]['tag']}"\n')
                    if old_lines != new_lines:
                        with open(pkgrev_file, 'w', newline='\n') as f:
                            f.writelines(new_lines)
                        support_repo.git.add(pkgrev_file)
                        support_repo.git.commit('-m', f"Update PV for {comp} for {ticket_number} issue {issue_number}")
                        support_repo.git.push('origin', support_branch)
                        print(f"[DEBUG] Updated PV field for {comp} in {pkgrev_file}")
                    else:
                        print(f"[DEBUG] No PV changes needed for {pkgrev_file}")
            except Exception as e:
                print(f"[DEBUG] Could not open or update support repo at {generic_support_path}: {e}")
        changes_made, support_changed, _ = update_bb_and_pkgrev(meta_repo_path, generic_support_path, updates)
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
        else:
            print("[DEBUG] No changes detected, PR will not be created for meta layer.")
        # Add support PR link if created
        # Create support layer PR if support_changed
        if support_changed:
            support_pr_title = f"[Auto] Update support layer for {ticket_number}"
            support_pr_description = build_pr_list_description(updates)
            support_pr_obj = create_pull_request(
                github_token,
                'rdkcentral/meta-middleware-generic-support',
                support_branch,
                base_branch,
                support_pr_title,
                support_pr_description
            )
        # ...existing code for summary issue...
    else:
        # Single PR: update PV for only its component in the topic branch
        feature_branch = f"topic/auto-{ticket_number.lower()}-pr-{pr_number}"
        meta_pr_title = f"[Auto] Update meta layer for {ticket_number} (PR {pr_number})"
        meta_pr_description = build_pr_list_description(updates)
        create_or_checkout_branch(Repo(meta_repo_path), feature_branch, base_branch)
        support_branch = f"topic/auto-support-{ticket_number.lower()}-pr-{pr_number}"
        support_repo = None
        if os.path.isdir(generic_support_path):
            try:
                support_repo = Repo(generic_support_path)
                create_or_checkout_branch(support_repo, support_branch, base_branch)
                # Only update PV field for the single component in generic-pkgrev.inc
                pkgrev_file = os.path.join(generic_support_path, 'conf', 'include', 'generic-pkgrev.inc')
                if os.path.exists(pkgrev_file):
                    comp = updates[0]['repo'].split('/')[-1]
                    pv_field = f'PV:pn-{comp}'
                    with open(pkgrev_file, 'r', newline='') as f:
                        old_lines = f.readlines()
                    new_lines = []
                    found_pv = False
                    for line in old_lines:
                        if line.strip().startswith(f'{pv_field} ='):
                            new_lines.append(f'{pv_field} = "{updates[0]['tag']}"\n')
                            found_pv = True
                        else:
                            new_lines.append(line)
                    if not found_pv:
                        new_lines.append(f'{pv_field} = "{updates[0]['tag']}"\n')
                    if old_lines != new_lines:
                        with open(pkgrev_file, 'w', newline='\n') as f:
                            f.writelines(new_lines)
                        support_repo.git.add(pkgrev_file)
                        support_repo.git.commit('-m', f"Update PV for {comp} for {ticket_number} PR {pr_number}")
                        support_repo.git.push('origin', support_branch)
                        print(f"[DEBUG] Updated PV field for {comp} in {pkgrev_file}")
                    else:
                        print(f"[DEBUG] No PV changes needed for {pkgrev_file}")
            except Exception as e:
                print(f"[DEBUG] Could not open or update support repo at {generic_support_path}: {e}")
        changes_made, support_changed, _ = update_bb_and_pkgrev(meta_repo_path, generic_support_path, updates)
        print(f"[DEBUG] changes_made: {changes_made}, support_changed: {support_changed}")
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
                auto_pr_links.append(f"- [Meta Video Auto PR]({meta_pr_obj.html_url})")
        else:
            print("[DEBUG] No changes detected, PR will not be created for meta layer.")
        # Add support PR link if created
    # --- Create summary issue only if more than one auto PR exists ---
    # Create summary issue only if both meta and support layer auto PRs exist
    if meta_pr_obj and support_pr_obj:
        auto_pr_links = []
        auto_pr_links.append(f"- [Meta Video Auto PR]({meta_pr_obj.html_url})")
        auto_pr_links.append(f"- [Meta Support Auto PR]({support_pr_obj.html_url})")
        issue_title = f"{ticket_number} - Auto PRs for rdkcentral/{comp_name}"
        issue_body = (
            f"## Automated Update Summary for {ticket_number}\n\n"
            f"### PRs Created:\n" +
            "\n".join(auto_pr_links) +
            "\n\n### Details:\n" +
            build_pr_list_description(updates)
        )
        create_summary_issue(github_token, meta_repo_name, issue_title, issue_body)
if __name__ == '__main__':
    main()
