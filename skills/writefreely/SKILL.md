---
name: writefreely
description: Draft, revise and publish posts on a self-hosted WriteFreely blog with the wfc CLI. Use when the user wants to write, edit, list, preview or publish WriteFreely blog posts.
---

# wfc: manage a WriteFreely blog from the terminal

wfc talks to the user's self-hosted WriteFreely blog through its API.

## Workflow

1. Write the post as a Markdown file. A first-line `# Heading` becomes the
   title. A trailing `---` line is dropped. Everything else is sent as-is.
2. `wfc new post.md` saves it as a draft and prints a preview URL.
   Drafts are unlisted, not private: anyone with the link can read one, but
   drafts are not listed on the blog, in feeds, or sent to followers.
3. Give the user the preview URL. Revise with `wfc update <id> post.md`
   (or `wfc update <id> --title "New title"`). Read back with
   `wfc show <id> --body-only`.
4. Only when the user asks to publish: `wfc publish <id> --blog <alias> --yes`.

## Rules

- Never run a risky command unless the user asked for that action in this
  conversation. Risky commands: publish, unpublish, edit-live,
  delete, pin, unpin, blog create/update/delete, account, login, logout.
- Risky commands refuse to run without `--yes` when no terminal is attached.
- Never put a password on the command line. The user runs `wfc login <url>`
  in their own terminal, or sets WF_PASSWORD.
- Use `--json` when you need to parse output.

## Commands

Read (safe):
  wfc whoami | blogs | blog show <alias>
  wfc list [--drafts | --blog <alias>]
  wfc show <id> [--body-only]
  wfc render <file|->
  wfc guide

Draft (safe, cannot make anything public):
  wfc new <file|-> [--title T]
  wfc update <id> [file|-] [--title T]       (drafts only)

Risky (ask the user first):
  wfc publish <id> --blog <alias> [--keep-date]   (re-dates to now by default)
  wfc unpublish <id>
  wfc edit-live <id> [file|-] [--title T] [--slug S]
  wfc delete <id>
  wfc pin <id> --blog <alias> [--position N] | wfc unpin <id> --blog <alias>
  wfc blog create <alias> [--title T] [--visibility V]
  wfc blog update <alias> --mathjax on|off --email-subs on|off [--title ...]
      [--visibility unlisted|public|private|protected]
      (both on|off flags are required: the server resets them on every update)
  wfc blog delete <alias>        (its posts become drafts)
  wfc account update --email E | wfc account password
  wfc login <url> | wfc logout

## Exit codes

0 ok, 1 usage error or refused, 2 API rejected the request,
3 login needed, 4 not found, 5 network or server error.
