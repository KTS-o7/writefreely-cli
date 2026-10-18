# writefreely-cli

`wfc` is a small command-line client for self-hosted [WriteFreely](https://writefreely.org) blogs. It is built so a coding agent (Claude Code, opencode) can draft, revise and publish posts for you, and so you never need the web editor.

- One Python file, standard library only, zero dependencies. Python 3.9+.
- Works with WriteFreely 0.16+ in multi-user and single-user mode.
- New posts are always drafts. Nothing becomes public except through `wfc publish`.

## Install

```sh
uvx writefreely-cli --help          # run without installing
uv tool install writefreely-cli     # or: pipx install writefreely-cli
```

From source: `uv tool install git+https://github.com/KTS-o7/writefreely-cli`.

## First run

Log in once, in your own terminal:

```sh
wfc login https://your.blog
```

It asks for your alias (username) and password. The token is saved in `~/.config/writefreely-cli/config.json` with mode 0600. The password is never stored, and is never accepted as a command-line argument.

## Workflow

```sh
wfc new post.md                     # save as a draft, prints a preview link
wfc show <id> --body-only           # read it back
wfc update <id> post.md             # revise (or: --title "New title")
wfc publish <id> --blog <alias>     # make it live
```

A first-line `# Heading` becomes the post title. A trailing `---` line is dropped. Line endings become `\n`. Everything else is sent exactly as written.

Drafts are **unlisted, not private**. WriteFreely has no per-post privacy. Anyone with a draft's link can open it, but drafts are not listed on your blog, in feeds, or sent to fediverse followers.

## Commands

| Tier | Commands |
|---|---|
| Read | `whoami`, `blogs`, `blog show <alias>`, `list [--drafts \| --blog <alias>]`, `show <id> [--body-only]`, `render <file>`, `guide` |
| Draft | `new <file\|->`, `update <id> [file\|-] [--title T]` (drafts only) |
| Risky | `publish <id> --blog <alias> [--keep-date]`, `unpublish <id>`, `edit-live <id> [file] [--title] [--slug]`, `delete <id>`, `pin`/`unpin <id> --blog <alias>`, `blog create\|update\|delete`, `account update\|password`, `login`, `logout` |

Read and draft commands cannot make anything public. Risky commands ask `[y/N]` in a terminal. Without a terminal (an agent's shell), they refuse unless `--yes` is given, so the risky action is visible in the exact command you approve.

Every command accepts `--json`. Run `wfc <command> --help` for all flags.

## Using it with Claude Code

1. Copy `skills/writefreely/` into `~/.claude/skills/`. Agents without the skill can run `wfc guide`, which prints the same instructions.
2. Let the agent run safe commands without asking, and keep risky ones on "ask". Add to `.claude/settings.json`:

```json
{
  "permissions": {
    "allow": [
      "Bash(wfc whoami)", "Bash(wfc whoami *)",
      "Bash(wfc blogs)", "Bash(wfc blogs *)",
      "Bash(wfc blog show *)",
      "Bash(wfc list)", "Bash(wfc list *)",
      "Bash(wfc show *)",
      "Bash(wfc render *)",
      "Bash(wfc guide)",
      "Bash(wfc new *)",
      "Bash(wfc update *)"
    ]
  }
}
```

Commands in different tiers never share a prefix (live edits are `edit-live`, not `update-…`), so a rule for a safe command cannot match a risky one.

## Using it with opencode or other agents

In `opencode.json` (the last matching rule wins, so the catch-all goes first):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "permission": {
    "bash": {
      "wfc *": "ask",
      "wfc whoami*": "allow",
      "wfc blogs*": "allow",
      "wfc blog show *": "allow",
      "wfc list*": "allow",
      "wfc show *": "allow",
      "wfc render *": "allow",
      "wfc guide*": "allow",
      "wfc new *": "allow",
      "wfc update *": "allow"
    }
  }
}
```

For any agent, add this to `AGENTS.md`:

```md
This project's blog is managed with the `wfc` CLI. Run `wfc guide` before using it.
Never run publish, unpublish, edit-live, delete or blog/account commands unless I asked for that action.
```

## Environment variables

| Variable | Use |
|---|---|
| `WF_PASSWORD` | Password for `login`, for logging in again automatically after a 401, and the current password for `account password` |
| `WF_NEW_PASSWORD` | New password for `account password` |
| `WF_BLOG_PASSWORD` | Password for `blog update --visibility protected` |
| `WF_ALIAS` | Alias for `login` |
| `WF_INSTANCE` | Which saved instance to use (same as `--instance`) |
| `WF_URL` + `WF_TOKEN` | Use this instance and token instead of the config file |
| `XDG_CONFIG_HOME` | Moves the config directory |
| `WFC_DEBUG` | Show internal details on unexpected errors (secrets stay redacted) |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | usage error, local error, or refused risky action |
| 2 | the API rejected the request |
| 3 | not logged in, or session expired |
| 4 | not found |
| 5 | network or server error |

## WriteFreely behaviour the CLI works around

- **Publishing a draft keeps the date it was first saved.** `wfc publish` sets the date to now first. Use `--keep-date` for a deliberately backdated post.
- **Every blog update switches MathJax and email subscriptions off** unless the request turns them on, and the API cannot read their current values. `wfc blog update` therefore requires `--mathjax on|off` and `--email-subs on|off` every time.
- Blog visibility is a bitmask (0 unlisted, 1 public, 2 private, 4 password-protected). The JSON key that sets it is `public`, not `visibility` as the API docs say.
- Publishing makes a new slug from the title. Change it afterwards with `wfc edit-live <id> --slug ...`.
- `GET /api/me/posts` is cached for 4 seconds on the server. `wfc` reads drafts from an uncached endpoint and retries once when needed.
- The server allows one login attempt per alias every 3 seconds.
- The API does not allow changing your username.

## Not covered

Instance admin pages, import, export, invites and stats. WriteFreely only allows these with a browser login, not an API token.
