The `.claude/skills/` folder mirrors `.agents/skills/` so Claude Code finds the
same skills. Edit the skills in `.agents/skills/` only, then run `python tools/sync_skills.py`;
`tests/e2e/test_skill_mirror.py` fails when the copy is out of date. The mirror is a copy, not a
symlink, so it works on Windows without extra git settings.
