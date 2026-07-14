## Summary

Describe the intended change and why it belongs in Personal Skill Hub.

## Security and compatibility

- [ ] No credentials, tokens, private keys, personal data, or unapproved Skill payloads are included.
- [ ] Immutable artifact, signature, approval, and audit guarantees remain fail-closed.
- [ ] Agent compatibility changes identify Formal, Preview, or Cloud Connector scope accurately.
- [ ] Database changes include an Alembic migration and rollback/restore consideration.

## Verification

- [ ] `pytest`
- [ ] `ruff check src tests migrations`
- [ ] `alembic check`
- [ ] OpenAPI snapshot regenerated and reviewed when the API contract changed.

> A source-code pull request is not authorization to publish any Skill to a GitHub Release.
> Every public Skill/version requires its own immutable authorization record in the Hub.
