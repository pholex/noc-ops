# Repository instructions

Read `CLAUDE.md` for repository conventions and `PUBLIC_CONTENT.md` for public-content requirements.

Before every commit, review the complete staged diff and proposed commit message against `PUBLIC_CONTENT.md`, then run `python3 scripts/check-public-content.py --staged`. Do not bypass hooks. Before pushing, also scan the outgoing commit range. Report only locations and categories of sensitive findings; never quote the matched content. Keep mandatory third-party copyright notices and attribution intact.
