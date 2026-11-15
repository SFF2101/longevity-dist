# Security and data boundaries

- Public repository: code, synthetic tests and documentation only. Never commit measurements,
  exports, runtime options, entity mappings for a real account, archive names or hashes, OAuth
  tokens or client secrets.
- CI has `contents: read` only, runs no deployment and holds no Home Assistant credentials.
- Home Assistant pulls releases through the Supervisor; there is no push path into Home Assistant.
- Report a vulnerability privately via GitHub security advisories.
