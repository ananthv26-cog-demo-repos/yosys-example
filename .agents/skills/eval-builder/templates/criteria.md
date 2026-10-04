# Would merge checklist, PR <number>

Grade the attempt as you would review a pull request from a teammate. Would merge means you would approve it with at most nitpicks. The hidden test result is shown to you, it is one input not the verdict.

## Blocking, any no means would not merge
Every blocking line ends with its source, `Source, the ask` (the engineer's original request) or `Source, existing behaviour`
(what the repository did before the change). A fact that exists only in the PR, a name the PR chose, a file it added, a
detail of how it was done, cannot be blocking, put it under Advisory or Not required.
- [ ] <requirement in the user's words>. Rules out <the plausible wrong answer, e.g. "hard coding the retry count" or "catching the exception and logging instead of retrying">. Source, the ask.
- [ ] <requirement>. Rules out <wrong answer>. Source, the ask.
- [ ] Does not break existing behaviour that the change touches. Rules out <e.g. "dropping the timeout when adding the retry">. Source, existing behaviour.

## Advisory, note but do not block
- [ ] <tests added or updated in the repo's own style>
- [ ] <naming and placement match the surrounding code>
- [ ] <no unrelated changes>

## Accepted alternatives
<implementations that differ from the real PR but should still pass, e.g. "a decorator instead of an inline loop is fine">

## Not required
<things the real PR did that an attempt need not do, e.g. "the changelog entry" or "the follow up refactor in utils.py">
