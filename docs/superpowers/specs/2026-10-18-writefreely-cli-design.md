# writefreely-cli — design

Date: 2026-10-18
Status: built as v0.1.0. The owner said to implement without further questions; this doc was updated to match what shipped.

## Intent

A small command-line client that lets the owner of a self-hosted WriteFreely blog manage it entirely from the terminal, without opening the web UI. The main user is a coding agent (Claude Code, opencode) acting for the owner; a person typing commands by hand is the second user.

The workflow it exists for:

1. The owner writes a post, by hand or by asking the agent to write it.
2. The agent saves it to the blog as a **draft**.
3. The owner opens the draft link on their own site to see it rendered.
4. The agent reads it back, revises it, and repeats as often as needed.
5. When the owner says so, the agent **publishes** it. Claude Code / opencode asks the owner to approve that one command.

It starts with the owner's instance (`https://think.shenthar.me`, WriteFreely 0.16.0, multi-user mode, blog alias `chanakya`) but is built for anyone running WriteFreely. It will be open source on GitHub (`KTS-o7/writefreely-cli`, private until first release) and published to PyPI as `writefreely-cli`.

It replaces copy-pasting into the web editor.

## Goals

- **Cover WriteFreely's token API completely** (posts, blogs, the owner's own account). "Group 1" in the brainstorm: everything an access token can do.
- **Tiny.** Python, standard library only, zero runtime dependencies. Installs in seconds, runs with `uvx`.
- **Agent-friendly.** Never hangs on a prompt when no one is at a terminal. `--json` on every command. Compact default output so agents spend few tokens. Stable exit codes. Error messages say what to do next. The tool can describe itself to an agent (`wfc guide`).
- **Safe by default.** Nothing becomes public except through a command whose only job is to make it public. Risky commands are separate verbs, so a coding agent's permission rules can allow the safe ones and keep the risky ones on "ask".

## Non-goals

- Anything that needs a browser login session rather than a token: instance admin (`/admin/*`), import, export, invites, stats, email-subscriber export. The server checks a cookie for these (`handle.go`, `getUserSession`) and ignores tokens. Possible v2.
- Server-shell operations (`writefreely user create`, `reset-pass`, `db migrate`). README may point to them; the CLI does not wrap them.
- Reading or writing the WriteFreely database, ever.
- Signup, ActivityPub/federation features, email subscriptions as a reader, the blog outbox.
- Local Markdown rendering, a TUI, a web UI.
- YAML front matter. A file that starts with front matter is sent as-is.
- Importing the owner's existing posts.

## Anti-goals (failure even if it technically works)

- A post goes public without the owner explicitly asking for that.
- A token or password shows up in stdout, stderr, a traceback, a log, `argv`, a URL, or the repo.
- Content is changed beyond the rules in "Content handling" (no smart quotes, no reflowing, no reformatting).
- A runtime dependency creeps in.
- A command waits for keyboard input when an agent is running it, and hangs.

## Facts about the WriteFreely API (verified)

Verified by reading the v0.16.0 source and probing `think.shenthar.me`. The builder should trust these over the public docs where they differ.

- Base path is `/api/`. `/v1/*` returns 404.
- Login: `POST /api/auth/login` with JSON `{"alias": ..., "pass": ...}` → 200 with `data.access_token`. The field is `alias`, not `username`.
- Auth header: `Authorization: Token <token>`, not `Bearer`.
- Errors: JSON `{"code": N, "error_msg": "..."}`. `code` is sometimes a string in examples; parse both.
- Success codes vary per endpoint: create post 201, update 200, delete 204, reads 200. Check per endpoint.
- **There is no per-post privacy.** The `posts.privacy` column is always written as 0 (`database.go:743`) and never read. Visibility belongs to the blog.
- **Blog visibility is a bitmask** stored in `collections.privacy`: 0 unlisted, 1 public, 2 private, 4 password-protected (`collections.go:155-160`). Checks use `&`. Same values on `develop` (0.17.x).
- **Setting visibility over JSON uses the key `"public"` with an integer value**, because the struct field is `Visibility *int` with tag `json:"public"` (`collections.go`, `SubmittedCollection`). The docs say `visibility`, which only works for form-encoded bodies. Setting 4 (protected) is ignored unless `password` is sent in the same request (`database.go:940`). Verify live before relying on it (see Acceptance).
- Reads only expose `"public": true/false` for a blog, not the full bitmask.
- **A draft is a post with no blog.** Drafts are unlisted, not private: `handleViewPost` and `GET /api/posts/{id}` do not check ownership for posts outside a blog. They are protected only by the random 10-character ID, are not listed anywhere, and are not federated. The CLI and docs must call them "drafts (unlisted)" and never "private".
- **Post update is partial.** `UpdateOwnedPost` only sets fields present in the request (`database.go:776`), and the handler does not require `body`. A title-only update works. Sending `title: ""` clears the title.
- Publishing a draft: `POST /api/collections/{alias}/collect` with `[{"id": ...}]` → 200 with a per-post result array, each with its own `code`. A 200 overall can still contain a failed item.
- Unpublishing: `POST /api/posts/disperse` moves posts back to drafts (`dispersePost`).
- **Deleting a blog does not delete its posts.** They move to drafts (`database.go:2455`).
- `GET /api/me/posts` accepts a token (`UserWebAPI`) and returns all the user's posts, drafts included.
- Account: `POST /api/me/self` takes `email`, `new_pass`, `current_pass` as JSON (`account.go`, `userSettings`). The JSON path blanks `username`, so the username cannot be changed through the API. `POST /api/me/password` reads form fields (`current`, `new`), so the CLI uses `/api/me/self` for password changes instead.
- **Every blog update clears MathJax and email subscriptions** unless the request sends `"mathjax": true` / `"email_subs": true` (`database.go:975`, `:1068`). No read endpoint returns either value.
- Blog `format` is a `sql.NullString` on the server, so JSON must send `{"String": "novel", "Valid": true}`. A plain string fails to decode.
- `collect` (publish) generates a new slug from the title and does not change `created`. A post drafted days earlier would appear with its draft date.
- Create takes `created` as `2006-01-02T15:04:05Z`; update takes `2006-01-02 15:04:05` (`postMetaDateFormat`).
- `GET /api/me/posts` is cached per user for 4 seconds and not cleared on writes (`cache.go`). `GET /api/me/posts?anonymous=1&page=0` returns all drafts uncached.
- Login allows one attempt per alias every 3 seconds (`loginAttemptExpiration`); a second attempt gets 429.
- Draft URLs are `/{id}` in multi-user mode and `/d/{id}` in single-user mode. `GET /api/me/collections` returns each blog's canonical `url`, which reveals the mode.
- Logout: `DELETE /api/auth/me` invalidates the token.

## The tool

### Name and packaging (my call)

- PyPI package and repo: `writefreely-cli`.
- Commands installed: `wfc` (short, used everywhere in docs) and `writefreely-cli` (so `uvx writefreely-cli ...` works without `--from`).
- Not `wf` (taken by the official writeas-cli) and not `writefreely` (the server binary's name).
- Python ≥ 3.9 (so macOS's system Python works), standard library only. Any build backend is fine; it is build-time only. Dev tools (lint, typecheck) may be used via `uvx` and must not become runtime dependencies.

### Commands, by safety tier

Tiers exist so that an agent's permission rules can match on the start of the command. Each command is its own top-level verb (or `blog …` / `account …` group) for that reason.

**Read tier** (safe to auto-allow):

| Command | API | Notes |
|---|---|---|
| `wfc whoami` | `GET /api/me` | Prints alias and instance. Never prints the token. |
| `wfc blogs` | `GET /api/me/collections` | Alias, title, public yes/no. |
| `wfc blog show <alias>` | `GET /api/collections/{alias}` | |
| `wfc list` | `GET /api/me/posts` | All of the user's posts, each marked `draft` or with its blog alias. |
| `wfc list --drafts` | same, filtered | |
| `wfc list --blog <alias>` | `GET /api/collections/{alias}/posts` | Pages through all results unless `--page` is given. |
| `wfc show <id>` | `GET /api/posts/{id}` | Metadata, URL, then the Markdown body. `--body-only` prints just the raw Markdown. |
| `wfc render <file\|->` | `POST /api/markdown` | Prints the server's HTML. |
| `wfc guide` | none | Prints the agent instructions (same text as the shipped skill). |

`list` never includes post bodies. Fields: id, slug, title, blog (or `draft`), created, updated.

**Draft tier** (safe to auto-allow; cannot make anything public):

| Command | API | Notes |
|---|---|---|
| `wfc new <file\|->` | `POST /api/posts` | Always creates a draft. Applies the content rules. Prints the id and the draft URL. Optional `--title`, `--created`, `--font`, `--lang`, `--rtl`. There is no `--blog` option. |
| `wfc update <id> [file\|-]` | `POST /api/posts/{id}` | **Drafts only.** Refuses a published post with a message pointing to `edit-live`. Sends only what was given: body (from the file), `--title`, `--created`, `--font`, `--lang`, `--rtl`. No `--slug`, because publishing regenerates the slug. |

**Risky tier** (leave on "ask"):

| Command | API | Notes |
|---|---|---|
| `wfc publish <id> --blog <alias> [--keep-date]` | `POST /api/posts/{id}` (created = now), then `POST /api/collections/{alias}/collect` | `--blog` is required; there is no default blog. Post must currently be a draft. Sets the date to now before publishing unless `--keep-date`. The confirmation text names the title, the target blog, whether it is public (visible to everyone and sent to fediverse followers), and the date handling. Checks the per-item result code. Prints the public URL. |
| `wfc unpublish <id>` | `POST /api/posts/disperse` | Back to drafts. Notes that fediverse followers keep the copy they already received. |
| `wfc edit-live <id> [file\|-]` | `POST /api/posts/{id}` | Same options as `update` plus `--slug`, for posts in a blog. Changes are live immediately. Named so it shares no prefix with `update`, keeping permission rules unambiguous. |
| `wfc delete <id>` | `DELETE /api/posts/{id}` | Hard delete. Prints the title before deleting. |
| `wfc pin <id> --blog <alias> [--position N]` / `wfc unpin <id> --blog <alias>` | `POST /api/collections/{alias}/pin` / `unpin` | |
| `wfc blog create <alias> [--title T]` | `POST /api/collections` | Note: the new blog starts at the instance's default visibility, which may be public. |
| `wfc blog update <alias> [...]` | `POST /api/collections/{alias}` | **Requires `--mathjax on\|off` and `--email-subs on\|off` every time** (see API facts). Optional: `--title`, `--description`, `--style-sheet <file>`, `--script <file>`, `--signature`, `--format blog\|novel\|notebook`, `--visibility unlisted\|public\|private\|protected`, `--monetization-pointer`, `--verification-link`, `--letter-reply`. A protected blog's password comes from a prompt or `WF_BLOG_PASSWORD`, never argv. |
| `wfc blog delete <alias>` | `DELETE /api/collections/{alias}` | States that its posts become drafts. |
| `wfc account update --email E` | `POST /api/me/self` | Email only; the API blocks username changes. |
| `wfc account password` | `POST /api/me/self` | Current and new password from prompts or `WF_PASSWORD` / `WF_NEW_PASSWORD`. |
| `wfc login <url>` / `wfc logout` | `POST /api/auth/login` / `DELETE /api/auth/me` | See Authentication. |

**Confirmation rule for every risky-tier command** (except login/logout):

- If stdin is a terminal and `--yes` is not given: show what will happen and ask `[y/N]`.
- If stdin is not a terminal and `--yes` is not given: refuse, exit 1, and say to re-run with `--yes`.
- With `--yes`: proceed.

The point of the non-terminal rule is not to stop agents. It makes the risky action visible as `--yes` in the exact command line the owner approves in Claude Code / opencode. The real gate is that approval.

### Content handling

Applies whenever a body is read from a file or stdin (`new`, `update`, `edit-live`, `render`):

1. Decode as UTF-8. Normalise CRLF and lone CR to LF.
2. If the first non-blank line is an ATX H1 (`# Title`), remove it and the blank lines right after it. Its text becomes the title, unless `--title` was given, in which case `--title` wins and the H1 is still removed.
3. Remove trailing horizontal-rule lines (`---` or longer, optionally with trailing spaces) at the end of the document, along with the blank lines around them. Repeat until the document no longer ends with one. A `---` directly under a text line is a setext heading underline, not a rule, and stays. `---` lines anywhere else are content and stay.
4. Remove trailing blank lines.
5. Send everything else byte-for-byte.

No title from H1 and no `--title` → no title is sent (WriteFreely derives one from the content).

Reference case: `~/reports/v3-post-body.md` has 159 lines, LF endings, an H1 on line 1, and 11 `---` lines. Only the last `---` (the final line) is trailing; the other 10 are section breaks and must survive.

### Authentication and config

- `wfc login <url>` asks for the alias (or takes `--alias`) and gets the password from a hidden prompt or `WF_PASSWORD`. Never from argv. Calls `POST /api/auth/login`, then `GET /api/me` once to confirm the token works, then saves.
- Refuse `http://` URLs (tokens would travel in clear text) except `localhost` / `127.0.0.1`.
- Config file: `$XDG_CONFIG_HOME/writefreely-cli/config.json`, defaulting to `~/.config/writefreely-cli/config.json` on every OS (my call; simpler than per-OS paths). Directory 0700, file 0600, written atomically. If an existing file is more permissive, tighten it and warn.
- Config holds one entry per instance (url, alias, token) plus which one is the default. `--instance <host>` or `WF_INSTANCE` picks another. The password is never stored.
- `WF_URL` + `WF_TOKEN` together override the config file entirely (for scripted or CI use).
- On a 401 from any call: if `WF_PASSWORD` is set, log in again, save the new token, and retry the call once. Otherwise exit 3 with "Session expired. Run `wfc login <url>` in a terminal." Do not prompt mid-command; an agent would hang.
- Login is a human step: the README tells people to run `wfc login` once in a normal terminal.
- The OS keychain is not used in v1, because reaching it needs a dependency.

### Output and errors

- Default output is compact plain text, one item per line for lists. Exact layout is the builder's call; it must stay readable by both people and agents.
- `--json` (accepted on every command) prints one JSON document to stdout with stable field names.
- Errors go to stderr as one line: what went wrong and what to do next. With `--json`, errors are `{"error": "...", "status": N}` on stderr.
- No tracebacks. A top-level handler catches everything. `WFC_DEBUG=1` may show more detail, but the `Authorization` header and any password are always redacted.
- Exit codes (my call; must be documented in `--help` and the guide):
  - 0 success
  - 1 usage error, local error, or refused risky action
  - 2 API rejected the request (4xx other than 401/404)
  - 3 not logged in or session expired
  - 4 not found
  - 5 network error or server error (5xx)
- URLs printed by `new`, `show` and `publish` should come from API response fields where available rather than being built by hand, since single-user and multi-user instances use different URL shapes.

### Agent integration

- `skills/writefreely/SKILL.md` in the repo: the workflow (draft → preview → iterate → publish only on request), the command list by tier, and the rule "never run a risky-tier command unless the owner asked for that action in this conversation".
- `wfc guide` prints the same text, so any agent can learn the tool from one command.
- README sections:
  - Install (`uvx writefreely-cli`, `pipx install writefreely-cli`).
  - First run (`wfc login` in a terminal).
  - A ready-to-paste Claude Code permissions block that allows the read and draft tiers and leaves the risky tier on ask.
  - The equivalent for opencode (builder to look up opencode's permission format).
  - An `AGENTS.md` snippet for other agents.

## Verification

The owner asked for zero automated tests in the repo. Each change is verified instead by:

- `uvx ruff check` and `uvx ruff format --check`, `uvx mypy` (strict), and `uv build`;
- running the CLI on macOS's system Python 3.9 and a current Python;
- an end-to-end run of every command against throwaway local WriteFreely 0.16.0 instances (one multi-user, one single-user) using a separate config directory, then scanning all output for the token and passwords;
- the live acceptance steps below.

No GitHub Actions workflow in v1 (the owner pays for CI minutes and wants to be asked first).

## Acceptance

Run against `https://think.shenthar.me`:

1. **Login.** `wfc login https://think.shenthar.me` succeeds. The config file exists with mode 0600. `wfc whoami` shows the alias.
2. **Real draft.** `wfc new ~/reports/v3-post-body.md` creates a draft titled "The fab is eighteen months late. The window isn't waiting." `wfc show <id> --body-only` shows:
   - no H1 line,
   - no trailing `---`,
   - all 10 interior `---` lines.

   The id appears in `wfc list --drafts` and does **not** appear in `GET /api/collections/chanakya/posts`. The owner publishes it later, by hand. It is not published as part of acceptance.
3. **Throwaway round trip.**
   - `new` a throwaway draft. It shows up in `list --drafts`.
   - `show` it.
   - `update --title` only, then `show` again: the body is unchanged.
   - `update` with a new body.
   - `delete --yes`. `show` now exits 4.
4. **Publish path without public exposure.** Create a throwaway blog:
   - `blog create`
   - with `--visibility private` (create and set visibility in one command)
   - `blog show` reports `public: false`

   Then:
   - `publish` a throwaway draft into it, and confirm it appears in `list --blog`
   - `edit-live`
   - `unpublish`
   - `blog delete`, and confirm the post is back in drafts
   - `delete` the post

   If the instance doesn't allow a second blog, skip step 4 live, rely on the local-instance run, and record that it was skipped.
5. **Secrets.** Capture all output from steps 1–4. Neither the token nor the password appears anywhere in it.
6. **Install.** From a fresh environment, `uvx --from dist/<wheel> wfc --help` works, and the wheel's metadata lists no runtime dependencies.

Account commands (`account update`, `account password`) are verified against the local instance only, not live.

## Release

- After acceptance passes:
  - The owner picks a license (MIT suggested).
  - The repo goes public.
  - `0.1.0` is built with `uv build` and published with `uv publish` from the owner's laptop, using the owner's PyPI credentials.
- No release workflow without asking.

## Left to the builder

- Module layout. One file is the target, but small enough is what matters.
- Exact plain-text output layout and help text.
- Flag spellings beyond the ones named here.
- How paging is implemented.
- Whether to add a `followers <alias>` read command, after checking what `GET /api/collections/{alias}/followers` returns on 0.16.0.
- Choice of lint, typecheck and build tools.

## Calls made without the owner (check these)

- Command name `wfc`, plus a `writefreely-cli` alias.
- Python ≥ 3.9, standard library only. This replaces the earlier Go suggestion, because the owner asked for PyPI and small.
- `update` refuses published posts. Live edits go through a separate risky-tier `edit-live`.
- Risky-tier commands require `--yes` when not run from a terminal.
- Login validates the token once with `GET /api/me`, instead of checking on every command start (saves a request per command).
- Config lives under `~/.config` on every OS, not in the keychain.
- Refuse `http://` instance URLs except localhost.
- `delete` ships in v1 (it is part of the token API the owner chose), in the risky tier.
- `publish` re-dates the post to now by default (`--keep-date` opts out). Added after a review found that publishing keeps the draft's date.
- `blog update` requires explicit `--mathjax` and `--email-subs` values, because the server clears both on every update and cannot report them.
- No automated tests (owner's instruction).
