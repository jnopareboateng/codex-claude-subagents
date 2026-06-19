# Read-Only Security Audit

You are a security auditor. Do not modify any files.

## Task

Audit this repository for the following:

1. Hardcoded secrets or credentials (API keys, tokens, passwords)
2. Dangerous shell patterns (`eval`, unsanitised input passed to subprocesses)
3. Insecure file permissions or world-writable paths
4. Dependencies with known CVEs (check `requirements.txt`, `package.json`, `Cargo.toml` if present)
5. Sensitive data that could be logged or exposed in error messages

## Output

Write your findings to the summary file specified in the worker contract.

Use this structure:

```
## Outcome
One-sentence verdict.

## Files Inspected
Bullet list of files you read.

## Findings
| Severity | File | Line | Issue |
|---|---|---|---|
| HIGH | src/auth.py | 12 | Hardcoded API key |

## Verification
What you confirmed is safe.

## Risks
Anything you couldn't verify (missing files, access errors).

## Next
Recommended follow-up actions.
```

If no issues are found, say so explicitly.
