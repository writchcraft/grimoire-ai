# Reporting vulnerabilities

Do not post secrets, private infrastructure details or exploitable deployments
in public issues. Use GitHub's private vulnerability reporting option on this
repository if enabled. If it is unavailable, open a minimal issue requesting a
private contact route without disclosing exploit details.

Include affected revision, reproduction steps using fictional data, expected
behavior and impact. Remove credentials from logs and attachments.

This initial single-user application has no authentication. Run it on loopback
or behind an authenticated TLS gateway. Read the [threat model](docs/security.md)
before deployment. Security fixes target the current `main` branch; no separate
maintenance release lines are currently defined.
