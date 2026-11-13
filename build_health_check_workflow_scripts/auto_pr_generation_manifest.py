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
import shutil


OSS_ORG = 'rdkcentral'
OSS_MANIFEST_REPO = 'rdke-oss-manifest'
OSS_MANIFEST_XML_FILES = ['rdk-oss.xml', 'rdk-docker.xml']
OSS_COMMON_MANIFEST_REPO = 'rdk-oss-common-manifest'
OSS_COMMON_MANIFEST_XML = 'rdk-oss-common.xml'
OSS_COMMON_LAYERS = {
  'meta-rdk-auxiliary', 'meta-oss-reference-development',
  'meta-rdk-oss-reference', 'meta-openembedded', 'poky',
  'meta-python2', 'meta-clang', 'meta-lts-mixins',
  'meta-rdk-bluetooth', 'meta-binder'
}


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
  xml_files = [f for f in os.listdir(manifest_repo_path) if f.endswith('.xml')]
  changes_made = False  # Flag to check if any changes are made

  for xml_file in xml_files:
    xml_path = os.path.join(manifest_repo_path, xml_file)
    print("Checking {}".format(xml_file))
    tree = ET.parse(xml_path)
    root = tree.getroot()
    file_changed = False

    for project in root.findall('project'):
      print("Checking projects {}".format(project.get('name')))
      if project.get('name') in updates and project.get('revision') != updates[project.get('name')]:
        print("Updating {}: {} from {} to {}".format(xml_file, project.get('name'), project.get('revision'), updates[project.get('name')]))
        project.set('revision', updates[project.get('name')])
        file_changed = True

    for submanifest in root.findall('submanifest'):
      print("Checking submanifests {}".format(submanifest.get('project')))
      if submanifest.get('project') in updates and submanifest.get('revision') != updates[submanifest.get('project')]:
        print("Updating {}: {} from {} to {}".format(xml_file, submanifest.get('project'), submanifest.get('revision'), updates[submanifest.get('project')]))
        submanifest.set('revision', updates[submanifest.get('project')])
        file_changed = True

    if file_changed:
        write_xml(root, xml_path)
        print("Updated {}".format(xml_file))
        changes_made = True

  if changes_made:
    repo.git.add(all=True)
  else:
    print("No changes were made to XML files.")

  return changes_made

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
        repo.git.commit('-m', commit_message)
        repo.git.push('origin', repo.active_branch.name)
    else:
        print("No changes to commit.")

#Create a new PR for the updated manifest files
def create_pull_request(github_token, repo_name, head_branch, base_branch, title, description):
    g = Github(github_token)
    repo = g.get_repo(repo_name)
    ensure_label_exists(repo, 'bhc-auto-merge', color='008672')

    try:
        pr = repo.create_pull(title=title, body=description, base=base_branch, head=head_branch)
        pr.add_to_labels('bhc-auto-merge')
        print("PR Created and labeled:", pr.html_url)
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

# Delete the unused branch in the remote manifest repository
def delete_branch(repo, branch_name):
    print("Delete unused branch: {}".format(branch_name))
    print(':{}'.format(branch_name))
    try:
        remote = repo.remote(name='origin')
        remote.push(refspec=(':{}'.format(branch_name)))
    except GitCommandError as e:
        print("Error delete branch: {}".format(str(e)))
        sys.exit(1)

def legacy_main():
    github_token = os.getenv('GITHUB_TOKEN')
    manifest_repo_path = os.getenv('MANIFEST_REPO_PATH')
    pr_number = os.getenv('PR_NUMBER')
    manifest_repo_name = os.getenv('MANIFEST_REPO_NAME')
    repo_name = os.getenv('GITHUB_REPOSITORY')
    repo_owner = os.getenv('GITHUB_ORG')
    base_branch = os.getenv('BASE_BRANCH')

    g = Github(github_token)
    repo = g.get_repo(repo_name)
    meta_pr = repo.get_pull(int(pr_number))

    # Extract ticket number
    ticket_number = extract_ticket_number(meta_pr.title)
 
    prs, issue_repo_name, issue_number = fetch_merge_commits(repo_owner, repo_name, int(pr_number), github_token)
    
    if issue_number:
        feature_branch = "feature_{}_issue_{}".format(issue_repo_name, issue_number)
        pr_title = "Auto PR for {} {}".format(issue_repo_name, issue_number)
    else:
        feature_branch = "feature_{}_pr_{}".format(repo_name.split('/')[-1], pr_number)
        pr_title = "Auto PR for {} {}".format(repo_name, pr_number)

    # Create or check out the branch in the local manifest repository
    repo = Repo(manifest_repo_path)
    create_or_checkout_branch(repo, feature_branch, base_branch)
    time.sleep(5)

    # Set the new PR title and description
    manifest_pr_title = "{} - {}".format(ticket_number, pr_title)
    pr_list = build_pr_list_description(prs)
    manifest_pr_description = "Details: {}\n{}".format(meta_pr.body, pr_list)

    updates = {pr['repo'].split('/')[-1]: pr['sha'] for pr in prs}

    print("Updates to be pushed to feature branch: {}".format(updates))
    
    changes_made = update_xml_files(manifest_repo_path, updates)
    if changes_made:  
        commit_and_push(manifest_repo_path, "Update manifest for {}".format(','.join(updates.keys())))
        create_pull_request(github_token, manifest_repo_name, feature_branch, base_branch, manifest_pr_title, manifest_pr_description)
    else:
        # delete the unused feature branch
        delete_branch(repo, feature_branch)


def update_oss_revision(file_path, project_name, revision):
    with open(file_path, 'r', encoding='utf-8') as xml_file:
        content = xml_file.read()
    pattern = (r'(<(?:project|submanifest)\s[^>]*?(?:name|project)=["\']{}["\'][^>]*?'
               r'revision=["\'])[^"\']*(["\'])').format(re.escape(project_name))
    updated_content, replacements = re.subn(pattern, r'\g<1>{}\g<2>'.format(revision), content)
    if not replacements:
        return False
    with open(file_path, 'w', encoding='utf-8') as xml_file:
        xml_file.write(updated_content)
    return updated_content != content


def clone_oss_manifest_repo(github_token, repo_name, workspace):
    os.makedirs(workspace, exist_ok=True)
    path = os.path.join(workspace, repo_name)
    if os.path.exists(path):
        shutil.rmtree(path)
    repo = Repo.clone_from('https://{}@github.com/{}/{}.git'.format(github_token, OSS_ORG, repo_name), path)
    repo.git.config('user.name', os.getenv('SVC_ACCOUNT_USERNAME', 'bot'))
    repo.git.config('user.email', os.getenv('SVC_ACCOUNT_EMAIL', 'bot@users.noreply.github.com'))
    return path, repo


def create_oss_pull_request(github_token, manifest_repo, branch, base_branch, title, description):
    repo = Github(github_token).get_repo('{}/{}'.format(OSS_ORG, manifest_repo))
    for label, color in (('auto-update', '0075ca'), ('manifest', '0075ca'), ('oss-auto-merge', '008672')):
        ensure_label_exists(repo, label, color)
    pr = repo.create_pull(title=title, body=description, base=base_branch, head=branch)
    pr.add_to_labels('auto-update', 'manifest', 'oss-auto-merge')
    print('OSS Auto PR created and labeled: {}'.format(pr.html_url))


def oss_branch_has_open_pr(repo, branch):
    return next(iter(repo.get_pulls(state='open', head='{}:{}'.format(OSS_ORG, branch))), None)


def oss_prepare_branch(repo, branch, base_branch):
    repo.git.fetch('origin')
    if 'origin/{}'.format(branch) in repo.git.branch('-r'):
        repo.git.checkout('-B', branch, 'origin/{}'.format(branch))
        repo.git.pull('origin', branch)
        return True
    repo.git.checkout(base_branch)
    repo.git.pull('origin', base_branch)
    repo.git.checkout('-b', branch)
    return False


def oss_update_manifest(manifest_path, xml_files, updates):
    changed = False
    for xml_name in xml_files:
        xml_path = os.path.join(manifest_path, xml_name)
        if not os.path.exists(xml_path):
            print('XML file not found, skipping: {}'.format(xml_path))
            continue
        for project_name, revision in updates.items():
            changed = update_oss_revision(xml_path, project_name, revision) or changed
    if changed:
        Repo(manifest_path).git.add(all=True)
    return changed


def oss_main():
    github_token = os.getenv('RDKCENTRAL_GITHUB_TOKEN') or os.getenv('GITHUB_TOKEN')
    source_repo_name = os.getenv('SOURCE_REPOSITORY') or os.getenv('GITHUB_REPOSITORY')
    pr_number = os.getenv('PR_NUMBER')
    base_branch = os.getenv('BASE_BRANCH')
    workspace = os.getenv('WORKSPACE') or os.getcwd()
    if not all([github_token, source_repo_name, pr_number, base_branch]):
        sys.exit('OSS mode requires token, SOURCE_REPOSITORY, PR_NUMBER, and BASE_BRANCH.')
    if not source_repo_name.startswith('{}/'.format(OSS_ORG)):
        sys.exit('Source repository {} is not in {}.'.format(source_repo_name, OSS_ORG))
    layer_name = source_repo_name.split('/')[-1]
    source_pr = Github(github_token).get_repo(source_repo_name).get_pull(int(pr_number))
    prs, _, _ = fetch_merge_commits(OSS_ORG, source_repo_name, int(pr_number), github_token)
    if not prs:
        print('No merge commits found. Nothing to process.')
        return
    updates = {pr['repo'].split('/')[-1]: pr['sha'] for pr in prs}
    new_sha = prs[0]['sha']
    manifest_type = os.getenv('MANIFEST_TYPE') or ('oss-common' if layer_name in OSS_COMMON_LAYERS else 'oss')
    manifest_repo, xml_files, target_base = ((OSS_COMMON_MANIFEST_REPO, [OSS_COMMON_MANIFEST_XML], 'develop')
                                             if manifest_type == 'oss-common'
                                             else (OSS_MANIFEST_REPO, OSS_MANIFEST_XML_FILES, base_branch))
    branch = 'auto-{}-{}'.format(layer_name, new_sha[:8])
    title = '[auto] Update {} to {}'.format(layer_name, new_sha[:8])
    ticket = extract_ticket_number(source_pr.title)
    if ticket != 'NO-TICKET':
        title = '{} : {}'.format(ticket, title)
    description = '{}{}'.format(source_pr.body or '', build_pr_list_description(prs))
    manifest_path, manifest_git_repo = clone_oss_manifest_repo(github_token, manifest_repo, os.path.join(workspace, 'oss_manifest_repos'))
    try:
        manifest_api_repo = Github(github_token).get_repo('{}/{}'.format(OSS_ORG, manifest_repo))
        if oss_branch_has_open_pr(manifest_api_repo, branch):
            print('Open OSS manifest PR already exists for {}.'.format(branch))
            return
        branch_exists = oss_prepare_branch(manifest_git_repo, branch, target_base)
        changed = oss_update_manifest(manifest_path, xml_files, updates)
        if not changed and not branch_exists:
            print('No OSS manifest changes required.')
            return
        if changed:
            commit_and_push(manifest_path, '[auto] Update {} to {}'.format(layer_name, new_sha[:8]))
        create_oss_pull_request(github_token, manifest_repo, branch, target_base, title, description)
    finally:
        shutil.rmtree(manifest_path, ignore_errors=True)


def main():
    if os.getenv('AUTO_PR_MODE') == 'oss':
        oss_main()
    else:
        legacy_main()


if __name__ == '__main__':
    main()
