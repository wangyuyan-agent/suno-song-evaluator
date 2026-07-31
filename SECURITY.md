# Security policy

## Supported version

Security fixes are applied to the latest `0.2.x` release.

## Reporting a vulnerability

Please use the private security-advisory flow at:

<https://github.com/wangyuyan-agent/suno-song-evaluator/security/advisories/new>

Do not include real songs, API keys, passwords, local filesystem paths, or
private Suno metadata in a public issue.

## Security model

- Local service mode binds to loopback and trusts only loopback Host headers.
- Remote binding is rejected unless it is explicitly enabled with an exact
  allowed Host and a password-file-backed administrator account.
- The base Compose deployment publishes only Caddy; the app port remains
  internal. Caddy provides HTTPS and security headers.
- The private Compose overlay publishes the app only on host loopback and
  relies on Tailscale Serve for tailnet-only HTTPS. It must not use Funnel.
- Linux deployment secrets are root-managed files readable only by the
  non-root container identity (numeric UID/GID 10001).
- Artifact, source, reference, and listening-media paths must resolve under
  configured trusted roots. Symlink escapes and historical out-of-root paths
  are rejected.
- Shared evidence exports redact local filesystem paths.
- The optional LLM API key is read from process configuration or a secret file.
  Requests cannot substitute another provider URL.

This release is single-administrator software, not a multi-tenant service. It
does not provide RBAC, self-service accounts, or isolation between mutually
untrusted users. Use a unique random password of at least 16 characters and
prefer a firewall allowlist, VPN, or identity-aware proxy for an Internet-facing
deployment; the built-in Basic Auth layer does not provide MFA or brute-force
rate limiting.
