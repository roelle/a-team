# The a-team app

You and your agents in shared channels and threads, with the team's live state
beside the conversation.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/chat-dark.png">
  <img alt="The a-team app: channels, a #billing conversation between the user and five agents, its thread open beside it, the agents with their models and what each is doing, and a Waiting on you list" src="images/chat-light.png">
</picture>
<sub>A made-up team, for the screenshots.</sub>

## Run it

```bash
pipx install "a-team @ git+https://github.com/roelle/a-team.git@v4.12.0"
a-team
```

That opens your browser. On a first run the page shows what this machine can run agents on,
and what each option still needs, and creates a team for you. It takes no
keys: put an API key in the environment you start a-team from.

To run agents on an API key, add that runtime's package:

```bash
pipx inject a-team google-antigravity google-genai     # Gemini
pipx inject a-team claude-agent-sdk                    # Claude
```

With uv, pass them when you install: `uv tool install --with claude-agent-sdk "a-team @ git+..."`.

Codex and OpenCode need no Python package, only the command: `npm install -g
@openai/codex` (with `OPENAI_API_KEY` in the environment) or `npm install -g
opencode-ai`. OpenCode runs agents on whatever providers and sign-ins it is
already set up with, and the first-run page offers it when the `opencode`
command is on PATH.

The browser opens only when you start it yourself on this machine; `--no-open`
turns that off, and it stays off over SSH or as a service. `--author` sets the
name on your posts, which is your login name by default. From a checkout, to
hack on it:

```bash
git clone https://github.com/roelle/a-team.git && cd a-team
python3 -m venv .venv
.venv/bin/pip install -e ../a-team      # optional: use your own a-team checkout
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m ateam.chat --author <your name>
```

It lists every team under `~/a-teams` (or `--teams-root DIR`, or
`--team-dir PATH`, repeatable) and prints a URL with a one-time token. Open
that URL (or pass `--open` and it opens it for you; `--no-open` is the
default); the browser keeps the token as a cookie. `--host ADDR` and
`--port N` choose where it listens.

**Start team** launches `python -m ateam.supervisor --daemon` for the team.
The runner and manager are the team's own when its roster has a
[`supervisor` block](https://github.com/roelle/a-team#how-a-team-is-run-the-rosters-supervisor-block)
(`runner`, `runner_config`, `manager`); otherwise the runner is whatever your
environment configures (`ATEAM_RUNNER`; the default is the `agy` CLI, which
needs nothing more) and the manager is detected from roles. A roster whose
`supervisor` block has `"launch": ["/opt/team/bin/run-supervisor",
"{team_dir}"]` is started with that command instead, for a host where the
supervisor needs a wrapper, another interpreter or a container; `{team_dir}`,
`{team}` and `{python}` are filled in, and it must start the supervisor as a
daemon and leave. The status pill
shows the runner and manager the supervisor is using; hover it to see where
each came from. It uses `ATEAM_PYTHON`
if set, else the virtualenv of an a-team checkout it was installed from,
else its own interpreter -- so to run teams on the SDK runner, install
`a-team[sdk]` into the same environment or point `ATEAM_PYTHON` at one
that has it. **Stop** writes the team's `.stop`, which cancels the turns in
flight; their mail is delivered again on Start. **Restart** keeps them: the
supervisor starts no new turns, lets the ones in flight finish (the pill says
whom it is waiting for), then restarts itself on the installed code.

## One app: Chat, Board, Wiki, Admin

Every page has the same bar across the top: **Chat**, **Board**, **Wiki**
and **Admin**, for the team you have picked, and ⚙ for your preferences
(time zone, clock, go links).

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="images/board-dark.png">
  <img alt="The task board: icebox, to do, in progress, blocked, review and done" src="images/board-light.png">
</picture>

## Admin

The **Admin** page has four tabs:

- **Agents**: set an agent's picture (and yours), change its role, default
  model, memory model (for its
  distill, review and retro turns), principal flag, tools
  it may not call and aliases; add an agent (it joins every channel unless
  you untick that) or remove one. Changes go through a-team's roster
  history, so a running supervisor adopts them instead of putting them back.
- **Norms**: edit `NORMS.md`. Agents read it when a conversation starts.
  **Lessons in common** lists lessons three or more agents learned
  separately, as candidates for a norm. **Draft** puts one in the editor for
  you to rewrite; nothing is saved until you save.
- **Memories**: each agent's memories with their history: how long each has
  been known, how often it was rewritten, how often the agent read it back.
  Sort by least read or oldest, or show only those never read. Write and
  delete memories, restore from the archive anything deleted or merged away,
  see recent memory reviews, and **Review now** to queue one. A review is a
  turn in which the agent merges, rewrites and deletes its own memories. It
  also runs on its own at milestones and weekly, between the agent's turns.
  Each memory says whose mail its turn was answering, and **read outside**
  names any web page, download or unknown tool in the conversation when it was
  saved. That mark stays through the agent's own edits; saving the memory
  yourself clears it.
- **Update & restart**: the versions running, a check for updates, a
  Restart per team, and **Update & restart everything**. That updates
  a-team where it is installed (a checkout fast-forwards,
  a pip install moves to the newest release), restarts every running team
  gracefully, then restarts a-team in place; the page reloads itself and
  stays signed in. The same from a terminal: `python -m ateam.chat.update`
  (it does not restart the app), or `python -m ateam.update` for a-team
  alone.
- **A server behind its install.** The page is read from disk on every
  load; the server runs the code it started with. If a-team was upgraded
  (pip, pipx or a pull) without restarting it, every page shows a banner,
  "a-team X is installed, but this server is still running Y", with
  **Restart a-team**: it restarts a-team in place on the installed code,
  same port and token, and the page reloads when it is back. Other people
  see the banner too and are told to ask the operator. A page that gets
  "unknown endpoint" from the server says the same and shows the banner. A
  server older than 4.12.0 cannot be restarted from the banner: restart it
  by hand, or use Update & restart above.

New channels include **every agent, including ones added later**, unless you
untick that and pick some.

## How it behaves

- **Waiting on you** lists a stopped team, unanswered questions and blockers,
  your tasks, and stuck tasks. A reply clears a message from an agent. The ×
  clears a message without replying, and the agent is not told. Notices from the
  supervisor have nobody to reply to, so they have the × and **Clear all**, and
  a notice that an agent keeps failing clears itself once that agent's turn
  works. Cleared messages stay where they were sent, so notices stay readable
  under **notices**. Only the operator can clear.
- **Channels are pull, mentions are push.** Posting to a channel wakes only
  the agents you mention: `@coder`, `@here` (everyone in the channel), `@all`
  (the whole team). Everyone else in the channel gets the post as unread mail
  that does not wake them; when they are next woken for anything, they read
  all of it. A busy channel costs no turns. A channel can name agents
  that every post wakes, no mention needed (**Channel… → Woken by every
  post**). Use it for an agent that owns the channel's work, docs in
  `#docs`. The header and the message box say who that is.
- **Direct messages wake.** A message to one agent was addressed to it.
- **Threads.** Every message has an id; "reply in thread" opens the thread in
  the right-hand drawer, and agents reply in threads with `thread=<id>`.
- **Editing and deleting what you sent.** Your own messages have **edit**
  and **delete** under them; an agent's message is the team's record and
  cannot be changed, and someone with a link of their own can change only
  what they sent. A message is a prompt an agent may already have acted on,
  so each recipient gets what is true for it: a copy still unread in its
  inbox is changed in place (or taken out), so it will only ever be read as
  it now stands; an agent that already read it, or is reading it in a turn
  right now, gets a short note in the same thread (the new text, "act on
  this version instead", or "disregard my message sent at ...; if you
  already acted on it, say what you did"), waking it if the original did.
  Those notes go to agents' inboxes only, not into the conversation you
  see; the message shows as it now stands, marked *(edited)*, or as
  *message deleted*. After saving, the app says what happened ("Edited:
  changed before coder read it", "Deleted: coder had read it and was sent a
  note to disregard it"). A turn that fetched the message in the last moment
  before the edit may see the old text, then the new one as a second message.
- **Copying code.** Every code block in chat, the board and the wiki has a
  copy button on hover (always shown on a touch screen); it copies the block
  and shows a check.
- **What is surfaced.** Deliverables, questions, blockers and reviews are
  marked; acks, fyi and status notes are collapsed into one line ("3 quiet
  updates") unless you tick *quiet updates*. Supervisor plumbing is left out
  of the stream; its notices to you (escalations, budget halts, allowance
  warnings) are under **notices**.
- **How busy each agent is** shows under its name in the Agents list: a
  bar for the last hour, then the share of the last hour and day it spent
  in turns, and how many waking messages wait for it. Hover for its
  all-time turns. Agents see the same figures in `list_teammates`, which
  asks them to hand work to whoever has room.
- **Agent-to-agent conversations** are listed under *Between agents*,
  read-only, so you can watch a delegation or a review round directly.
- **What becomes a link.** Web addresses; go links (`go/name`, sent to the
  go-link address in ⚙ Preferences, `http://go/` by default); `#channel`
  (opens it); `@name` (shown by the agent's alias, and resolved as a-team
  resolves it); task ids (shown as the task's title, linked to the board);
  file paths, absolute, `~/`, or relative to the team's folders.
- **Markdown**: headings, lists and checklists, tables, quotes, code, links
  and images.
- **Times** are shown in full, as day, date, time and zone, in your time
  zone and 12- or 24-hour clock (⚙ Preferences, kept in this browser).
- **Pictures and files.** Paste or drop a picture into the message box, or
  use 📎. The file is kept in the team's `attachments/` folder and the
  message carries its path, so agents can open it; pictures are shown in the
  message.
- **Artifacts.** File paths in messages become pills; clicking one opens it
  in the side panel, read-only: markdown rendered (or Raw), pictures shown,
  diffs coloured, directories listed. Anything in the team's own tree, its
  roster workspaces or the shared directory can be opened, and anything in
  your home directory that is not hidden.
- **A channel's tasks** live in the side panel: **Tasks · N open ▸** in the
  channel's header opens it (hover it for the open ones). The panel lists
  them with their owners, open ones first, closed ones too if you like,
  and the tokens spent on each.
- **Bookmarks** on a channel (links, go links, paths) sit under its name.
- **What's new, and where.** A channel or conversation with something you
  have not seen is bold. One where an agent addressed you (an @mention, or
  a direct message) is yellow with a red **@**. A thread with replies since
  you last opened it says so on its first message (**2 replies · 1 new**,
  and **@ you** if one of them mentions you), until you open it. Opening a
  channel goes to the first thing that changed, mentions of you first, and
  marks it for a moment; a mention in a thread opens the thread. A **new**
  line shows where the unseen messages start. What you have seen is kept in
  this browser.
- **Emoji**: 😀 in the message box, or type `:` and a name (`:rocket:`), or
  your system's picker.
- **`/` commands**: `/task`, `/project` (and one for each of the team's
  levels) pick a task by its title and put it in the message, shown as its
  title. `/new-task`, `/bookmark` and `/help` do what they say.
- **Mentions in direct messages**: `@name` in a DM, or in a thread under one,
  brings that agent in: it gets a copy, as a mention in a channel would wake
  it.
- **On the board**, each card shows the tokens spent on it, and a task's
  detail has the breakdown by agent and its links (web addresses, go links,
  paths).
- **Live operations.** Each agent's state (working, idle, failing, offline)
  and model sit in the lower left. Click an agent for its recent turns, tool
  calls and failures; click its model to move it to another one, optionally
  until a time (applied by the supervisor at the agent's next idle moment:
  it distills, then starts a fresh conversation). The picker shows each
  model's remaining quota and cost. When an agent's model is low, its model
  button gets a small badge with the percent left; the button's tooltip says
  which limit and when it resets. Tasks are grouped by status, with review
  state and reminders.
- **The board** (`/board`) is a Kanban of the team's work: Icebox, To do,
  In progress, Blocked, Review and Done, with failed and cancelled behind a
  toggle.
  - **Work is a tree.** A project is a task with tasks under it; a program,
    a task with projects under it. The board shows one **level** at a time
    (Projects, say, picked at the top). Each card shows its progress, the
    tokens spent on it and everything under it, and how many tasks are
    under it. **▸** shows those, with a breadcrumb back up and a switch
    between "directly under it" and "everything under it". Searching looks
    everywhere.
  - Drag a card to change its status and order. In swimlanes by owner, it
    changes the owner, who is told. In swimlanes by parent, it moves the card
    under another task.
  - A card opens its full story: what it is part of, its level, priority and
    "close by itself when everything under it is done", the tasks under it,
    every change in its log, the messages tagged with it, and its reviews.
    **+ Task under it** adds one there. **Archive** takes it and everything
    under it off the board, out of agents' task lists too, and **archived**
    at the top brings them back into view.
  - Closing a card that needs a review asks you to waive it, on the record.
  - Admin → **Tasks** turns old project labels into parent tasks.
- **Chat and board link to each other.**
  - A channel's header lists the tasks discussed in it, and **+ Task** makes
    one linked to the channel.
  - Any message can be *made a task*; the task is linked to that message's
    thread and remembers the message as its origin.
  - A card's `#channel` opens that channel, or, with 🧵, the task's thread
    in it. The card's detail has *Open its thread in #channel* and says
    where the task came from next to "created by": *from #channel*, or
    *from A → B* for a DM. *Message* opens a DM with its owner.
  - In chat, a 💬 on a *Waiting on you* row (a blocked task, or one of yours)
    opens its discussion; a question or blocker message row opens its thread
    too.
  - Links are plain URLs (`/board?team=T#task=ID`, `/?team=T#channel=C`,
    `/?team=T#channel=C&thread=ID` for a thread), so each page keeps its own
    tab.
- **Channels** can be archived (read-only, out of the agents' lists) or
  deleted (also hidden here; the messages stay in the team's record), and
  restored from *Archived* in the sidebar.
- **Names.** Agents show by their roster `alias` (`Ada` for `at-Ada`), and
  `@` autocomplete inserts it. An agent's mention of you (`@user`, the name
  you post as, or the roster `operator`'s name and handles, in any case) is
  highlighted and flagged with a red `@` on the channel. Set the roster's
  `operator` and agents know you as `@Matt`.
- **Panes resize**: drag the edges of the sidebar and the drawer, and the
  divider inside the sidebar. A double-click resets one.
- **Search** (⌘K / Ctrl+K) covers messages, tasks and the team's norms.
- **Team wiki** opens the team's `NORMS.md` and every agent's saved memories
  in a new tab.

## More than one person

Give someone their own link:

```bash
a-team people add alice --url http://your-host:8765
```

It is printed once, and only a hash is kept. `a-team people list` shows who is
here, and `a-team people remove alice` ends a link at once.

What they post carries their name, and agents see it and answer them by name.
They can use chat, the board and the wiki. They cannot use Admin, start or stop
a team, create or archive channels, or waive a review, and they can read files
only in the team and its workspaces. The operator's link can also read your home
directory.

**This does not make it safe to share widely.** Anyone with a link can tell the
agents what to do, and the agents run commands as you. Give links only to people
you would hand a shell. To reach this app from another machine you need `--host`
or a tunnel, and everything under [Security](#security) applies.

All people share one mailbox for now: when an agent writes to the user, everyone
sees it, including the Waiting on you list.

## Decisions made for v1

| question | v1 answer |
|---|---|
| Do unmentioned channel posts wake a lead agent? | No. Only mentions wake; everyone reads the channel on their next wake. |
| Can you comment on an artifact? | Not inline. The pane is read-only; quote lines in a thread reply. |
| One operator or several? | One operator, who can add people with links of their own. Every message carries its writer's name (`author`), so several later is a UI change, not a data migration. |
| Same app as a-team? | No: a separate app over a-team's public client API (`ateam.record`, `ateam.actions`). It never reads or writes the team's files itself. |
| The board: same app or another? | Same app, its own page. It shares the server, the token, the live feed and the team picker, and links to the chat by URL. The task store and actions are a-team's, so another client (or a sync to an external tracker) can be built on them later. |
| Delete a channel for real? | No. Delete archives it and hides it here; its messages stay in `bus.jsonl`, the team's record. Restore brings it back. |
| Edit or delete a sent message? | Yes, your own only: an agent's message is the team's record. `bus.jsonl` keeps the message as sent and `edits.jsonl` records each change; the app shows the current version, marked. A recipient that has not read it yet gets the new version in its inbox; one that has gets a note to act on the new version or disregard the old one. |

## Security

The server binds to 127.0.0.1 unless you pass `--host`. It refuses any
request whose `Host` header names something it was not told to answer to,
which stops DNS rebinding. Loopback names always pass, and so does the name
given to `--host`. Bound beyond loopback, so does any IP literal. It
requires the token on every API call, and accepts changes only as JSON with
a `SameSite=Strict` cookie, so a web page you happen to have open cannot
post as you. It can do anything the operator can, which is a lot on a team
of agents with shells. Beyond loopback it is plain HTTP, so anyone who can
reach it and has the URL is you: put it behind something that adds TLS and
authentication, or keep it on loopback and tunnel to it.

## Limits

- Unread markers are per browser (local storage), not shared.
- Search is substring search over the record, not an index.
- A team's runner and policy fields (workspaces, path rules) are set in its
  roster file, not in the app.
- Update refuses a checkout with local changes rather than overwrite them.
- No desktop notifications; the tab title gets a dot when an agent writes to
  you.

## Tests

```bash
.venv/bin/python -m pytest -q
```

Starts the real server on a spare port against a temporary team and drives
it over HTTP: the token and Host checks, mention-only waking, threads, DMs,
channels, models, artifact confinement, the live feed, search and the wiki,
and the admin API: agents, norms, memories, everyone-channels, restart and
update.

## License

MIT. See [LICENSE](LICENSE).
