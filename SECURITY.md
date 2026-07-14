# Security policy

Version 0.1 is a personal MVP. Report suspected vulnerabilities through GitHub's private
vulnerability-reporting form in the repository Security tab before public disclosure. If that form
is unavailable, contact the repository owner out of band; do not open a public issue containing
exploit details. Never include live tokens, private keys, customer data, or proprietary Skill
packages in an issue.

The following are security boundaries, not optional features:

- private Skill metadata is filtered before search results are returned;
- uploaded archives remain in quarantine until validation succeeds;
- published versions and their content digests are immutable;
- the CLI verifies the downloaded digest and manifest before installation;
- public GitHub export requires explicit authorization for that exact version;
- secrets must be supplied out-of-band and must never be committed.
