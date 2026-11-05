# data-retrieval-service

data-retrieval-service is a JupyterHub hub-managed service that lets users retrieve their archived home directories.

Users go to `/services/data-retrieval/` on the hub and see every archive that belongs to them, grouped by hub and newest first. Clicking **Download** signs a short-lived URL and redirects the browser to it, so the file downloads straight from Google Cloud Storage. This replaces the GitHub data retrieval issue in [berkeley-dsep-infra/datahub](https://github.com/berkeley-dsep-infra/datahub). 


## Configuration

data-retrieval-service is configured via traitlets. The service can be configured either through command-line arguments or environment variables.

Below is an example of how to configure the data-retrieval-service:

```python

c.JupyterHub.services = [
    {
        'name': "data-retrieval",
        'url': "http://hub:10101",
        'command': ["data-retrieval", '--url_expiration=3600', '--cache_ttl=86400'],
        'oauth_no_confirm': True,
        'environment': {
            'DATA_RETRIEVAL_SA': '{service account JSON key}',
        },
    }
]

```

Users need the `access:services` scope (or `access:services!service=data-retrieval`) to use the service. The service itself doesn't need any extra scopes, since it only uses the hub to identify the logged-in user.

### Argument Descriptions

- `--bucket`: GCS bucket with the archived home directories.
- `--email_domain`: Domain appended to hub usernames to get the user's email address. Default: `berkeley.edu`.
- `--support_email`: Support email. Default: `datahub-support@berkeley.edu`.
- `--url_expiration`: How long a signed download URL is valid, in seconds. Default: `3600`.
- `--cache_ttl`: How long to cache a user's list of archives, in seconds. Default: `86400` (1 day).
- `--max_concurrent_lookups`: Limit the number of GCS lookups running at the same time. Default: `4`.
- `--rate_limit`: Number of downloads allowed per user per `rate_window`. Default: `5`.
- `--rate_window`: The rate limit window, in seconds. Default: `3600`.

## Development

```
pip install -e ".[test]"
pytest
```
