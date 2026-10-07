# Read-Only Security Audit

Audit this repository. You are in review mode: read files and run read-only commands only.

## Check for

1. Hardcoded secrets or credentials (API keys, tokens, passwords)
2. Dangerous shell patterns (`eval`, unsanitised input passed to subprocesses)
3. Insecure file permissions or world-writable paths
4. Dependencies with known CVEs (`requirements.txt`, `package.json`, `Cargo.toml` if present)
5. Sensitive data that could be logged or exposed in error messages

## Report

- One finding per issue, with severity, file, line, the evidence you saw, and a concrete
  recommendation. Use `line: null` when an issue is not tied to one line.
- `verdict`: `request-changes` if any finding is high or critical, `approve` if there are none,
  `inconclusive` if you could not inspect enough of the repo to say.
- `verification`: what you checked and found safe, with the commands you ran.
- `risks`: what you could not verify (missing files, access errors).
