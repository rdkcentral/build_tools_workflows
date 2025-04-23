import sys
import requests
import os
import time

def parse_github_issue_url(url):
    """
    Parse a GitHub Issue URL of the form:
      https://github.com/rdk-e/meta-rdk/issues/123
    Returns a tuple: ('rdk-e', 'meta-rdk', '123')
    """
    parts = url.strip().rstrip('/').split('/')
    # Expect something like ["https:", "", "github.com", "owner", "repo", "issues", "123"]
    if len(parts) < 7:
        raise ValueError(f"URL not in expected format: {url}")
    owner = parts[3]
    repo_name = parts[4]
    issue_number = parts[6]
    return owner, repo_name, issue_number

def fetch_pr_details(repo_name, pr_number, github_token, max_attempts=10):
    # Define API URL and headers
    url = 'https://api.github.com/graphql'
    headers = {
      "Authorization": "bearer {}".format(github_token),
      "Content-Type": "application/json"
    }

    # Define the GraphQL query for a single PR
    query = """
    query($org: String!, $repo: String!, $number: Int!) {
      repository(owner: $org, name: $repo) {
        pullRequest(number: $number) {
          number
          mergeable
          labels(first: 10) {
            nodes {
              name
            }
          }
          reviews(last: 1) {
            nodes {
              state
            }
          }
          headRepository {
            nameWithOwner
          }
        }
      }
    }
    """
    
    # Variables for the GraphQL query
    org, repo = repo_name.split('/')
    variables = {"org": org, "repo": repo, "number": int(pr_number)}

    # Attempt to fetch PR details with retry logic
    for attempt in range(max_attempts):
        response = requests.post(url, json={'query': query, 'variables': variables}, headers=headers)
        if response.status_code == 200:
            pr = response.json()['data']['repository']['pullRequest']
            if pr['mergeable'] != 'UNKNOWN':
                return pr
        time.sleep(2)  # Wait for 2 seconds before retrying

    return None

def merge_pull_request(repo, pr_number, token):
    url = "https://api.github.com/repos/{}/pulls/{}/merge".format(repo, pr_number)
    headers = {
        "Authorization": "token {}".format(token),
        "Content-Type": "application/json"
    }
    response = requests.put(url, headers=headers)
    return response.status_code, response.json()

# Modified function to fetch both CONNECTED_EVENT and DISCONNECTED_EVENT nodes
def get_linked_pull_requests_details(repo_name, issue_number, github_token, owner):
    # GitHub GraphQL API URL
    url = 'https://api.github.com/graphql'

    # GraphQL query modified to fetch both CONNECTED_EVENT and DISCONNECTED_EVENT
    query = """
    query($org: String!, $repo: String!, $number: Int!) {
      repository(owner: $org, name: $repo) {
        issue(number: $number) {
          timelineItems(itemTypes: [CONNECTED_EVENT, DISCONNECTED_EVENT], last: 100) {
            nodes {
              __typename
              ... on ConnectedEvent {
                subject {
                  ... on PullRequest {
                    number
                    title
                    url
                    baseRefName
                    headRefName
                    mergeable
                    labels(first: 10) {
                      nodes {
                        name
                      }
                    }
                    reviews(last: 1) {
                      nodes {
                        state
                      }
                    }
                    headRepository {
                      nameWithOwner
                    }
                  }
                }
              }
              ... on DisconnectedEvent {
                subject {
                  ... on PullRequest {
                    number
                    title
                    url
                    baseRefName
                    headRefName
                    mergeable
                    labels(first: 10) {
                      nodes {
                        name
                      }
                    }
                    reviews(last: 1) {
                      nodes {
                        state
                      }
                    }
                    headRepository {
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
    """

    # Variables for the GraphQL query
    variables = {
        "org": owner,
        "repo": repo_name,
        "number": int(issue_number)
    }

    # Request headers
    headers = {
        "Authorization": f"bearer {github_token}",
        "Content-Type": "application/json"
    }

    # Execute POST request
    response = requests.post(url, json={'query': query, 'variables': variables}, headers=headers)
    
    if response.status_code != 200:
        raise Exception(f"GraphQL query failed with status {response.status_code}. Response: {response.text}")

    data = response.json()

    # We'll use two sets to track pull requests:
    # - connected_pr_keys: keys from CONNECTED_EVENT nodes.
    # - disconnected_pr_keys: keys from DISCONNECTED_EVENT nodes.
    #
    # The unique key for a pull request is defined as:
    #   (pr['headRepository']['nameWithOwner'], pr['number'])
    connected_pr_keys = set()
    disconnected_pr_keys = set()
    
    # A dictionary to hold the pull request details (for CONNECTED_EVENT nodes).
    pr_details_dict = {}
    
    nodes = data["data"]["repository"]["issue"]["timelineItems"]["nodes"]
    for node in nodes:
        event_type = node.get("__typename")
        pr = node.get("subject")
        # Sanity check: if pr is None or missing expected fields, skip it.
        if not pr or pr.get("headRepository") is None:
            continue

        # Define a unique key for the PR.
        pr_key = (pr["headRepository"]["nameWithOwner"], pr["number"])

        if event_type == "ConnectedEvent":
            connected_pr_keys.add(pr_key)
            # Prepare the details dictionary as in your original function.
            labels = [label['name'] for label in pr['labels']['nodes']]
            review_states = [review['state'] for review in pr['reviews']['nodes']]
            detail = {
                "repo": pr['headRepository']['nameWithOwner'],
                "base_branch": pr['baseRefName'],
                "pr_number": pr['number'],
                "feature_branch": pr['headRefName'],
                "mergeable": pr['mergeable'],
                "verified_label": "CCI-Verified" in labels,
                "review_approved": "APPROVED" in review_states
            }
            # In case multiple CONNECTED_EVENT nodes exist for the same PR,
            # the later one (or first one encountered) is stored.
            pr_details_dict[pr_key] = detail

        elif event_type == "DisconnectedEvent":
            disconnected_pr_keys.add(pr_key)

    # Use set difference: PRs that are connected and not disconnected.
    current_pr_keys = connected_pr_keys - disconnected_pr_keys

    # Build final list of PR details using the keys.
    pr_details = [pr_details_dict[key] for key in current_pr_keys if key in pr_details_dict]
    
    return pr_details

def can_merge(pr, override_verified):
    """
    Returns True if this PR passes all required checks:
      - Must be MERGEABLE
      - Must have an APPROVED review
      - Must have 'Verified' label unless override is True
    """
    if override_verified:
        # Skip 'Verified' label check
        return (pr['mergeable'] == 'MERGEABLE' and
                pr['review_approved'])
    else:
        # 'Verified' label is required
        return (pr['mergeable'] == 'MERGEABLE' and
                pr['verified_label'] and
                pr['review_approved'])


def main():
    """
    Entry point for the merge script. Expects the environment variables:
      - ISSUE_URL: full URL to the GH issue
      - OVERRIDE_VERIFIED: 'true' or 'false'
      - GITHUB_TOKEN: your GitHub PAT or Actions token
    """
    issue_url = os.environ.get('ISSUE_URL')
    if not issue_url:
        sys.exit("ERROR: 'ISSUE_URL' environment variable is not set.")

    token = os.environ.get('GITHUB_TOKEN')
    if not token:
        sys.exit("ERROR: 'GITHUB_TOKEN' environment variable is not set.")

    # Parse the Issue URL to get (owner, repo_name, issue_number)
    try:
        owner, repo_name, issue_number = parse_github_issue_url(issue_url)
    except ValueError as e:
        sys.exit(f"ERROR parsing Issue URL: {str(e)}")

    pr_details = get_linked_pull_requests_details(repo_name, issue_number, token, owner)
    unmergeable_details = []

    # Check all PRs for criteria, with retry for mergeable status
    for pr in pr_details:
        # Fetch details with retry mechanism for 'mergeable' status
        pr_retry_details = fetch_pr_details(pr['repo'], pr['pr_number'], token)
        if pr_retry_details:
            pr['mergeable'] = pr_retry_details['mergeable']
            pr['labels'] = [label['name'] for label in pr_retry_details['labels']['nodes']]
            pr['reviews'] = [review['state'] for review in pr_retry_details['reviews']['nodes']]
            pr['verified_label'] = "CCI-Verified" in pr['labels']
            pr['review_approved'] = "APPROVED" in pr['reviews']
            pr['base_branch'] = pr_retry_details.get('baseRefName', pr['base_branch'])
        
        # If PR does not meet the merging criteria
        if not can_merge(pr, override_verified=True):
            unmergeable_details.append(
                f"PR #{pr['pr_number']} in {pr['repo']} cannot be merged. "
                f"Base Branch: {pr['base_branch']}, Mergeable: {pr['mergeable']}, "
                f"Mergeable: {pr['mergeable']}, Verified: {pr['verified_label']}, "
                f"Approved: {pr['review_approved']}"
            )

    if unmergeable_details:
        for message in unmergeable_details:
            print(message)
        sys.exit("Stopping workflow due to one or more pull requests not meeting the criteria.")

    # Otherwise, merge all PRs that pass the checks
    for pr in pr_details:
        if can_merge(pr, override_verified=True):
            status_code, response = merge_pull_request(pr['repo'], pr['pr_number'], token)
            print(
                f"Merge PR {pr['repo']} #{pr['pr_number']}: "
                f"HTTP {status_code} - {response.get('message', 'No message')}"
            )

if __name__ == "__main__":
    main()
