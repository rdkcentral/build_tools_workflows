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
            # Recursively search for entservices-*.bb under meta-rdk-video/recipes-extended/entservices/
            entservices_dir = os.path.join(manifest_repo_path, 'recipes-extended', 'entservices')
            if os.path.isdir(entservices_dir):
                for root, dirs, files in os.walk(entservices_dir):
                    for f in files:
                        if f == f'entservices-{comp.split("entservices-")[-1]}.bb':
                            bb_file = os.path.join(root, f)
                            break
                    if bb_file:
                        break
            # For support layer PR
            support_entservices_dir = os.path.join(generic_support_path, 'recipes-extended', 'entservices')
            if os.path.isdir(support_entservices_dir):
                for root, dirs, files in os.walk(support_entservices_dir):
                    for f in files:
                        if f == f'entservices-{comp.split("entservices-")[-1]}.bb':
                            support_bb_file = os.path.join(root, f)
                            break
                    if support_bb_file:
                        break
            pkgrev_file = os.path.join(generic_support_path, 'conf', 'include', 'generic-pkgrev.inc')
            pkgrev_pv_field = f'PV:pn-{comp}'
        elif repo_name == 'rdk-e/rdkservices-cpc':
            bb_file = os.path.join(manifest_repo_path.replace('meta-middleware-generic-support', 'meta-rdk-comast-video'), 'rdkservices-comcast.bb')
            cspc_support_path = generic_support_path.replace('meta-middleware-generic-support', 'meta-middleware-cspc-support')
            support_bb_file = None  # Not handled for support layer in this case
            pkgrev_file = os.path.join(cspc_support_path, 'conf', 'include', 'cspc-pkgrev.inc')
            pkgrev_pv_field = 'rdkservices-comcast_PV'
        else:
            print(f"[DEBUG] Skipping repo {repo_name} (not handled)")
            continue
        print(f"[DEBUG] bb_file: {bb_file}")
        print(f"[DEBUG] pkgrev_file: {pkgrev_file}")
        # Update .bb file SRCREV and PV in meta layer
        if bb_file and os.path.exists(bb_file):
            with open(bb_file, 'r') as f:
                lines = f.readlines()
            file_changed = False
            tag_to_use = tag if tag else '1.0.0'
            with open(bb_file, 'w') as f:
                for line in lines:
                    if line.strip().startswith('SRCREV ='):
                        print(f"[DEBUG] Updating SRCREV in {bb_file} to {sha}")
                        f.write(f'SRCREV = "{sha}"\n')
                        file_changed = True
                    elif line.strip().startswith('PV ?='):
                        print(f"[DEBUG] Updating PV in {bb_file} to {tag_to_use}")
                        f.write(f'PV ?= "{tag_to_use}"\n')
                        file_changed = True
                    else:
                        f.write(line)
            if file_changed:
                repo.git.add(bb_file)
                changed = True
                print(f"[DEBUG] Changed {bb_file}")
            else:
                print(f"[DEBUG] No change needed for {bb_file}")
        else:
            print(f"[DEBUG] .bb file does not exist: {bb_file}")

        # Update .bb file SRCREV in support layer (if present)
        if support_repo and support_bb_file and os.path.exists(support_bb_file):
            with open(support_bb_file, 'r') as f:
                lines = f.readlines()
            file_changed = False
            with open(support_bb_file, 'w') as f:
                for line in lines:
                    if line.strip().startswith('SRCREV ='):
                        print(f"[DEBUG] Updating SRCREV in {support_bb_file} to {sha}")
                        f.write(f'SRCREV = "{sha}"\n')
                        file_changed = True
                    else:
                        f.write(line)
            if file_changed:
                support_repo.git.add(support_bb_file)
                support_changed = True
                print(f"[DEBUG] Changed {support_bb_file}")
            else:
                print(f"[DEBUG] No change needed for {support_bb_file}")
        elif support_bb_file:
            print(f"[DEBUG] Support .bb file does not exist: {support_bb_file}")

        # Only update pkgrev_file in support layer repo
        tag_to_use = tag if tag else '1.0.0'
        if pkgrev_file and os.path.exists(pkgrev_file) and support_repo:
            with open(pkgrev_file, 'r') as f:
                lines = f.readlines()
            file_changed = False
            with open(pkgrev_file, 'w') as f:
                for line in lines:
                    if line.strip().startswith(f'{pkgrev_pv_field} ='):
                        print(f"[DEBUG] Updating PV in {pkgrev_file} to {tag_to_use}")
                        f.write(f'{pkgrev_pv_field} = "{tag_to_use}"\n')
                        file_changed = True
                    else:
                        f.write(line)
            if file_changed:
                support_repo.git.add(pkgrev_file)
                support_changed = True
                print(f"[DEBUG] Changed {pkgrev_file}")
            else:
                print(f"[DEBUG] No change needed for {pkgrev_file}")
        elif pkgrev_file:
            print(f"[DEBUG] pkgrev_file does not exist or support_repo not found: {pkgrev_file}")
    print(f"[DEBUG] update_bb_and_pkgrev changed={changed}, support_changed={support_changed}")
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
        if not user_name:
            config_writer.set_value('user', 'name', os.environ.get('GIT_COMMITTER_NAME', 'github-actions[bot]'))
        if not user_email:
            config_writer.set_value('user', 'email', os.environ.get('GIT_COMMITTER_EMAIL', 'github-actions[bot]@users.noreply.github.com'))
        config_writer.release()
        repo.git.commit('-m', commit_message)
        repo.git.push('origin', repo.active_branch.name)
    else:
        print("No changes to commit.")

#Create a new PR for the updated manifest files
def create_pull_request(github_token, repo_name, head_branch, base_branch, title, description):
    g = Github(github_token)
    repo = g.get_repo(repo_name)

    try:
        # Only create the PR, do NOT merge it. Manual review/merge is required.
        pr = repo.create_pull(title=title, body=description, base=base_branch, head=head_branch)
        print("PR Created:", pr.html_url)
        return pr
    except github.GithubException as e:
        print("Failed to create PR:", str(e))
        return None

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

        # Check if the branch exists in the remote repository
        existing_branches = repo.git.branch('-r')
        if 'origin/{}'.format(branch_name) in existing_branches:
            print("Branch {} already exists remotely. Stopping further processing.".format(branch_name))
            sys.exit(1)  # Exit with a non-zero code to signal that the branch already exists
        else:
            # Switch to 'base_branch' and pull the latest changes to ensure local repo is up-to-date
            repo.git.checkout(base_branch)
            repo.git.pull('origin', base_branch)

            # Create and check out the new branch locally
            repo.git.checkout('-b', branch_name)
            print("Created and checked out new branch: {}".format(branch_name))

            # Push the newly created branch to the remote
            repo.git.push('origin', branch_name)

    except GitCommandError as e:
        print("Error checking out branch: {}".format(str(e)))
        sys.exit(1)

def get_tag_for_sha(github_token, repo_full_name, sha):
    """
    Return the tag name for a given repo and commit SHA, or None if not found.
    """
    g = Github(github_token)
    repo = g.get_repo(repo_full_name)
    tags = repo.get_tags()
    for tag in tags:
        if tag.commit.sha.startswith(sha):
            return tag.name
    return None

def main():
    github_token = os.getenv('GITHUB_TOKEN')
    manifest_repo_path = os.getenv('META_REPO_PATH')
    generic_support_path = os.getenv('GENERIC_SUPPORT_PATH')  # new env var for meta-middleware-generic-support
    pr_number = os.getenv('PR_NUMBER')
    manifest_repo_name = os.getenv('META_REPO_NAME')
    repo_name = os.getenv('GITHUB_REPOSITORY')
    repo_owner = os.getenv('GITHUB_ORG')
    base_branch = os.getenv('BASE_BRANCH')

    # Check required environment variables
    if not generic_support_path:
        print("ERROR: GENERIC_SUPPORT_PATH environment variable is not set.")
        sys.exit(1)
    if not manifest_repo_path:
        print("ERROR: META_REPO_PATH environment variable is not set.")
        sys.exit(1)
    if not github_token:
        print("ERROR: GITHUB_TOKEN environment variable is not set.")
        sys.exit(1)
    if not pr_number:
        print("ERROR: PR_NUMBER environment variable is not set.")
        sys.exit(1)
    if not manifest_repo_name:
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
    # For each PR, get the tag from GitHub
    print(f"[DEBUG] PRs to process: {prs}")
    updates = []
    for pr in prs:
        tag = get_tag_for_sha(github_token, pr['repo'], pr['sha'])
        print(f"[DEBUG] Tag for {pr['repo']} at {pr['sha']}: {tag}")
        updates.append({'repo': pr['repo'], 'sha': pr['sha'], 'tag': tag})

    print("Updates to be pushed to feature branch: {}".format(updates))

    # Generate a feature branch name and PR title/description
    feature_branch = f"auto-update-{ticket_number.lower()}"
    manifest_pr_title = f"[Auto] Update meta layer for {ticket_number}"
    manifest_pr_description = build_pr_list_description(updates)

    # Switch to feature branch BEFORE making changes
    create_or_checkout_branch(Repo(manifest_repo_path), feature_branch, base_branch)

    changes_made, support_changed, support_repo = update_bb_and_pkgrev(manifest_repo_path, generic_support_path, updates)
    print(f"[DEBUG] changes_made: {changes_made}, support_changed: {support_changed}")
    if changes_made:
        commit_and_push(
            manifest_repo_path,
            "Update manifest and pkgrev for {}".format(', '.join([f"{u['repo'].split('/')[-1]}:{u['sha'][:7]}:{u['tag'] if u['tag'] else '1.0.0'}" for u in updates]))
        )
        create_pull_request(
            github_token,
            manifest_repo_name,
            feature_branch,
            base_branch,
            manifest_pr_title,
            manifest_pr_description
        )
    else:
        print("[DEBUG] No changes detected, PR will not be created for meta layer.")

    # Support layer PR
    if support_changed:
        support_branch = f"auto-update-support-{ticket_number.lower()}"
        support_pr_title = f"[Auto] Update support layer for {ticket_number}"
        support_pr_description = build_pr_list_description(updates)
        if support_repo:
            create_or_checkout_branch(support_repo, support_branch, base_branch)
            commit_and_push(
                generic_support_path,
                "Update support layer entservices SRCREV for {}".format(', '.join([f"{u['repo'].split('/')[-1]}:{u['sha'][:7]}:{u['tag'] if u['tag'] else '1.0.0'}" for u in updates]))
            )
            create_pull_request(
                github_token,
                'rdkcentral/meta-middleware-generic-support',
                support_branch,
                base_branch,
                support_pr_title,
                support_pr_description
            )
        else:
            print("[DEBUG] No support_repo found for PR creation.")
# ...existing code...
if __name__ == '__main__':
    main()
