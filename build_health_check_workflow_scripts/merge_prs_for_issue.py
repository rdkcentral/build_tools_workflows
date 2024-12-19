import sys
import requests
import os
import time

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

import requests

def get_linked_pull_requests_details(repo_name, issue_number, github_token, owner):
    # GitHub GraphQL API URL
    url = 'https://api.github.com/graphql'

    # GraphQL query modified to fetch mergeable state along with other details
    query = """
    query($org: String!, $repo: String!, $number: Int!) {
      repository(owner: $org, name: $repo) {
        issue(number: $number) {
          timelineItems(itemTypes: [CONNECTED_EVENT], last: 100) {
            nodes {
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
        "Authorization": "bearer {}".format(github_token),
        "Content-Type": "application/json"
    }

    # Execute POST request
    response = requests.post(url, json={'query': query, 'variables': variables}, headers=headers)
    
    # Process response
    if response.status_code == 200:
        data = response.json()
        #print(data)
        pr_details = []
        for node in data["data"]["repository"]["issue"]["timelineItems"]["nodes"]:
            pr = node["subject"]
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
            pr_details.append(detail)
        
        return pr_details
    else:
        raise Exception("GraphQL query failed with status {}. Response: {}".format(response.status_code, response.text))

def main():
    issue_number = sys.argv[1]
    repo = sys.argv[2]
    token = os.environ.get('RDKCM_RDKE').strip()
    owner, repo_name = repo.split('/')

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
        
        if not (pr['mergeable'] == 'MERGEABLE' and pr['verified_label'] and pr['review_approved']):
        # if not (pr['mergeable'] == 'MERGEABLE' and pr['review_approved']):
            unmergeable_details.append("PR #{} in {} cannot be merged. Mergeable: {}, CCI-Verified: {}, Approved: {}".format(pr['pr_number'], pr['repo'], pr['mergeable'], pr['verified_label'], pr['review_approved']))

    if unmergeable_details:
        for message in unmergeable_details:
            print(message)
        sys.exit("Stopping workflow due to one or more pull requests not meeting the criteria.")

    # If all are mergeable, proceed with merging
    for pr in pr_details:
        if pr['mergeable'] == 'MERGEABLE' and pr['verified_label'] and pr['review_approved']:
        #if pr['mergeable'] == 'MERGEABLE' and pr['review_approved']:
            status_code, response = merge_pull_request(pr['repo'], pr['pr_number'], token)
            print("Merge PR {} {}: HTTP {} - {}".format(pr['repo'], pr['pr_number'], status_code, response.get('message', 'No message')))

if __name__ == "__main__":
    main()
