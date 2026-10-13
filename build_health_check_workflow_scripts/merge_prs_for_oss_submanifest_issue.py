import requests
import os
import sys

GITHUB_ORG = "rdkcentral"

def merge_pull_request(repo, pr_number, token):
    """Squash merge a pull request in an rdkcentral OSS manifest repo."""
    url = "https://api.github.com/repos/{}/pulls/{}/merge".format(repo, pr_number)
    headers = {"Authorization": "token {}".format(token),
               "Content-Type": "application/json"}
    payload = {
        "commit_title": "[auto] Squash merge OSS manifest PR #{}".format(pr_number),
        "commit_message": "OSS manifest auto merge",
        "merge_method": "squash"
    }
    response = requests.put(url, headers=headers, json=payload)
    if response.status_code == 200:
        print("Successfully squash merged PR #{} in {}.".format(pr_number, repo))
    else:
        print("Failed to merge PR #{} in {}: {}".format(
            pr_number, repo, response.json().get('message')))
    return response.status_code, response.json()


def close_issue(repo, issue_number, token):
    url = "https://api.github.com/repos/{}/issues/{}".format(repo, issue_number)
    headers = {"Authorization": "token {}".format(token),
               "Content-Type": "application/json"}
    response = requests.patch(url, json={"state": "closed"}, headers=headers)
    if response.status_code == 200:
        print("Issue #{} in {} has been closed.".format(issue_number, repo))
    else:
        print("Failed to close Issue #{} in {}: HTTP {}".format(
            issue_number, repo, response.status_code))


def get_linked_pull_requests(repo_name, issue_number, github_token):
    """Fetch PRs linked to an issue (ConnectedEvent + CrossReferencedEvent), rdkcentral only."""
    url = 'https://api.github.com/graphql'
    query = """
    query($owner: String!, $repo: String!, $number: Int!) {
        repository(owner: $owner, name: $repo) {
            issue(number: $number) {
                timelineItems(last: 100) {
                    nodes {
                        ... on ConnectedEvent {
                            subject {
                                __typename
                                ... on PullRequest {
                                    number
                                    state
                                    mergeable
                                    repository { nameWithOwner }
                                }
                            }
                        }
                        ... on CrossReferencedEvent {
                            source {
                                __typename
                                ... on PullRequest {
                                    number
                                    state
                                    mergeable
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
    variables = {"owner": GITHUB_ORG,
                 "repo": repo_name.split('/')[-1],
                 "number": int(issue_number)}
    headers = {"Authorization": "bearer {}".format(github_token),
               "Content-Type": "application/json"}
    response = requests.post(url, json={'query': query, 'variables': variables}, headers=headers)
    if response.status_code != 200:
        raise Exception("Failed to fetch linked PRs. Status: {}, Response: {}".format(
            response.status_code, response.text))

    nodes = response.json()["data"]["repository"]["issue"]["timelineItems"]["nodes"]
    pr_details, seen = [], set()
    for node in nodes:
        pr = None
        if node.get('subject', {}).get('__typename') == 'PullRequest':
            pr = node['subject']
        elif node.get('source', {}).get('__typename') == 'PullRequest':
            pr = node['source']
        if not pr or not pr.get('number'):
            continue
        repo = pr['repository']['nameWithOwner']
        key = (repo, pr['number'])
        # Only rdkcentral, only open, no duplicates
        if repo.split('/')[0] != GITHUB_ORG or pr.get('state') != 'OPEN' or key in seen:
            continue
        seen.add(key)
        pr_details.append({"repo": repo, "pr_number": pr['number'], "mergeable": pr['mergeable']})
    return pr_details


def main(repo, issue_number, token):
    pr_details = get_linked_pull_requests(repo, issue_number, token)
    print("Found {} open rdkcentral PR(s) linked to issue #{}.".format(len(pr_details), issue_number))

    all_merged = True
    for pr in pr_details:
        status_code, response = merge_pull_request(pr['repo'], pr['pr_number'], token)
        if status_code != 200:
            print("Failed to merge PR #{} in {}: HTTP {} - {}".format(
                pr['pr_number'], pr['repo'], status_code,
                response.get('message', 'No specific error message available')))
            all_merged = False

    if all_merged:
        close_issue(repo, issue_number, token)
    else:
        print("Not all PRs were merged. Issue #{} will remain open for manual review.".format(
            issue_number))
        sys.exit(1)


if __name__ == "__main__":
    github_token = (os.getenv('GITHUB_TOKEN') or '').strip()
    if not github_token:
        print("ERROR: GITHUB_TOKEN environment variable is not set.")
        sys.exit(1)

    repo         = os.getenv('REPO_NAME')
    issue_number = os.getenv('ISSUE_NUMBER')

    if not all([repo, issue_number]):
        print("ERROR: REPO_NAME and ISSUE_NUMBER must be set.")
        sys.exit(1)

    if repo.split('/')[0] != GITHUB_ORG:
        print("ERROR: {} is not in {} org.".format(repo, GITHUB_ORG))
        sys.exit(1)

    print("Repo: {}, Issue: {}, Owner: {}".format(repo, issue_number, GITHUB_ORG))
    main(repo, issue_number, github_token)
