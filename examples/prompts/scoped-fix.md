# Scoped Fix

You are a scoped Claude worker. You may only write files within the directory specified in your worker contract.

## Task

Investigate and fix the issue described below. Confirm the fix is complete before writing your summary.

---

<!-- Replace this section with the actual task description before running. -->

**Issue**: [Describe the bug or problem here]

**Acceptance criteria**:
- [ ] Root cause identified
- [ ] Fix applied within the allowed write scope
- [ ] Existing tests still pass (or new test added if appropriate)
- [ ] No unrelated files modified

---

## Output

Write your results to the summary file specified in the worker contract.

Use this structure:

```
## Outcome
One-sentence result.

## Files Inspected
Bullet list of files you read.

## Files Changed
Bullet list of files you modified (with one-line reason each).

## Verification
How you confirmed the fix works.

## Risks
Anything incomplete, untested, or uncertain.

## Next
Any follow-up work required.
```
