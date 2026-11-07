import os
import sys
import requests

GITHUB_ORG = "rdkcentral"


def merge_pull_request(repo, pr_number, token):
    """Squash merge a pull request in an rdkcentral repository."""
    url = "https://api.github.com/repos/{}/pulls/{}/merge".format(repo, pr_number)
    headers = {
        "Authorization": "token {}".format(token),
        "Content-Type": "application/json"
    }
    payload = {
        "commit_title": "[auto] Squash merge OSS manifest PR #{}".format(pr_number),
        "commit_message": "OSS manifest auto merge",
        "merge_method": "squash"
    }
    response = requests.put(url, headers=headers, json=payload)
    body = {}
    try:
        body = response.json()
    except Exception:
        body = {"message": response.text}

    if response.status_code == 200:
        print("Successfully squash merged PR #{} in {}.".format(pr_number, repo))
    else:
        print("Failed to merge PR #{} in {}: {}".format(
            pr_number, repo, body.get('message', response.text)))
    return response.status_code, body


def close_issue(repo, issue_number, token):
    """Close a GitHub issue."""
    url = "https://api.github.com/repos/{}/issues/{}".format(repo, issue_number)
    headers = {
        "Authorization": "token {}".format(token),
        "Content-Type": "application/json"
    }
    data = {"state": "closed"}
    response = requests.patch(url, json=data, headers=headers)
    if response.status_code == 200:
        print("Issue #{} in {} has been closed.".format(issue_number, repo))
    else:
        print("Failed to close Issue #{} in {}: HTTP {} - {}".format(
            issue_number, repo, response.status_code, response.text))


def get_linked_pull_requests(repo_name, issue_number, github_token):
    """
    Fetch ALL currently-linked PRs from the issue timeline, with pagination,
    and with connect/disconnect state replayed in timeline order.

    - Includes CONNECTED_EVENT, DISCONNECTED_EVENT, CROSS_REFERENCED_EVENT
    - Replays state so disconnected PRs are excluded
    - Keeps all states (OPEN/CLOSED/MERGED) so caller can enforce:
      "close issue only when every linked PR is MERGED"
    """
    url = 'https://api.github.com/graphql'

    query = """
    query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
      repository(owner: $owner, name: $repo) {
        issue(number: $number) {
          timelineItems(
            first: 100,
            after: $cursor,
            itemTypes: [CONNECTED_EVENT, DISCONNECTED_EVENT, CROSS_REFERENCED_EVENT]
          ) {
            pageInfo {
              hasNextPage
              endCursor
            }
            nodes {
              __typename
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
              ... on DisconnectedEvent {
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

    headers = {
        "Authorization": "bearer {}".format(github_token),
        "Content-Type": "application/json"
    }

    linked = {}       # (repo, pr_number) -> bool current link state
    explicit = set()  # keys that had explicit Connected/Disconnected events
    pr_info = {}      # (repo, pr_number) -> latest PR payload
    cursor = None

    while True:
        variables = {
            "owner": GITHUB_ORG,
            "repo": repo_name.split('/')[-1],
            "number": int(issue_number),
            "cursor": cursor
        }
        response = requests.post(
            url,
            json={"query": query, "variables": variables},
            headers=headers
        )
        if response.status_code != 200:
            raise Exception("Failed to fetch linked PRs. Status: {}, Response: {}".format(
                response.status_code, response.text))

        body = response.json()
        if "errors" in body:
            raise Exception("GraphQL errors: {}".format(body["errors"]))

        timeline = body["data"]["repository"]["issue"]["timelineItems"]

        # With first/after pagination, nodes are oldest -> newest.
        # Replay events in order to compute final link state.
        for node in timeline["nodes"]:
            event_type = node.get("__typename")

            pr = None
            if event_type in ("ConnectedEvent", "DisconnectedEvent"):
                pr = node.get("subject")
            elif event_type == "CrossReferencedEvent":
                pr = node.get("source")

            if not pr or pr.get("__typename") != "PullRequest" or not pr.get("number"):
                continue

            repo_full = pr["repository"]["nameWithOwner"]
            key = (repo_full, pr["number"])
            pr_info[key] = pr

            if event_type == "ConnectedEvent":
                linked[key] = True
                explicit.add(key)
            elif event_type == "DisconnectedEvent":
                linked[key] = False
                explicit.add(key)
            else:
                # Cross-reference only sets linked if no explicit connect/disconnect exists
                if key not in explicit:
                    linked[key] = True

        if not timeline["pageInfo"]["hasNextPage"]:
            break
        cursor = timeline["pageInfo"]["endCursor"]

    pr_details = []
    for key, is_linked in linked.items():
        if not is_linked:
            continue

        repo_full, pr_number = key
        if repo_full.split('/')[0] != GITHUB_ORG:
            continue

        pr = pr_info[key]
        pr_details.append({
            "repo": repo_full,
            "pr_number": pr_number,
            "state": pr.get("state"),           # OPEN / CLOSED / MERGED
            "mergeable": pr.get("mergeable"),   # MERGEABLE / CONFLICTING / UNKNOWN
        })

    return pr_details


def main(repo, issue_number, token):
    pr_details = get_linked_pull_requests(repo, issue_number, token)
    print("Found {} currently linked rdkcentral PR(s) for issue #{}.".format(
        len(pr_details), issue_number))

    all_merged = True

    for pr in pr_details:
        state = pr.get("state")

        if state == "MERGED":
            print("PR #{} in {} is already MERGED.".format(pr["pr_number"], pr["repo"]))
            continue

        if state == "OPEN":
            status_code, response = merge_pull_request(pr["repo"], pr["pr_number"], token)
            if status_code == 200:
                print("Merged PR #{} in {}.".format(pr["pr_number"], pr["repo"]))
                continue

            print("Failed to merge PR #{} in {}: HTTP {} - {}".format(
                pr["pr_number"], pr["repo"], status_code,
                response.get("message", "No specific error message available")))
            all_merged = False
            continue

        # CLOSED (not merged) or any unknown state
        print("PR #{} in {} is {} (not MERGED).".format(
            pr["pr_number"], pr["repo"], state))
        all_merged = False

    if all_merged:
        close_issue(repo, issue_number, token)
    else:
        print("Not all linked PRs are merged. Issue #{} remains open for manual review.".format(
            issue_number))
        sys.exit(1)


if __name__ == "__main__":
    github_token = (os.getenv("GITHUB_TOKEN") or "").strip()
    if not github_token:
        print("ERROR: GITHUB_TOKEN environment variable is not set.")
        sys.exit(1)

    repo = os.getenv("REPO_NAME")
    issue_number = os.getenv("ISSUE_NUMBER")

    if not all([repo, issue_number]):
        print("ERROR: REPO_NAME and ISSUE_NUMBER must be set.")
        sys.exit(1)

    if repo.split('/')[0] != GITHUB_ORG:
        print("ERROR: {} is not in {} org.".format(repo, GITHUB_ORG))
        sys.exit(1)

    print("Repo: {}, Issue: {}, Owner: {}".format(repo, issue_number, GITHUB_ORG))
    main(repo, issue_number, github_token)