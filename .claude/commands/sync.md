---
description: Sync with teammates: fetch, rebase, summarize what changed, flag conflicts.
---
Run `git fetch --all`, then `git status -sb` and `git log --oneline HEAD..origin/main` (if a remote exists). If the branch is clean, `git pull --rebase`; otherwise commit or stash first. Never force-push. If there are conflicts, list files and propose the resolution but do not apply it without confirmation. Reply in Russian, <=8 lines: what changed from teammates, risks for our files.
