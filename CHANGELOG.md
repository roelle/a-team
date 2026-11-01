# Changelog

All notable changes to agy-chat. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Before 1.0.0, a minor release may
break; the public surface is the command line and the HTTP API under /api.

## [Unreleased]

## [0.17.1] - 2026-11-01

### Fixed
- The README's install command named 0.16.0 in the 0.17.0 release.

## [0.17.0] - 2026-11-01

### Added
- **Waiting on you.** The top of the sidebar lists what needs you: a stopped
  team, questions and blockers from agents with no reply yet, tasks you own,
  and tasks an agent marked blocked. It takes no space when nothing is waiting.
  `/api/state` returns it as `needs_you`. Requires agy-team 3.22.0.
- **Task dependencies on the board.** A card shows what it waits for, and a
  task's detail sets **Waits for** and **Expects to write**.

## [0.16.0] - 2026-10-31

### Added
- **Admin shows what channel mail costs.** Under Agents, **Channel mail**
  says how many posts that mentioned nobody reached agents in the last 7
  days, about how many tokens that was, and how much came from each
  channel. `/api/admin` returns it as `quiet_mail`. Requires agy-team 3.19.0.

## [0.15.0] - 2026-10-29

### Added
- **A channel can wake some agents on every post.** In **Channel…**, tick
  agents under "Woken by every post, no @mention needed". Every post
  there wakes them, from you or from another agent, without an @mention.
  Ticking an agent who is not in the channel adds them. The channel
  header says "every post wakes docs", and the message box says who a
  post will wake. `/api/channel` takes `action: "wake"` with `agents`.
  Requires agy-team 3.17.0.

## [0.14.0] - 2026-10-29

### Added
- **How busy each agent is.** Under each name in the Agents list: a bar
  for the last hour, then "81% hr · 14% day · 2 waiting", the share of the
  last hour and day spent in turns and the waking messages not yet picked
  up. Hover for all-time turns and hours. These are the figures agents now
  get from `list_teammates`, which asks them to hand work to whoever has
  room. `/api/state` carries them as each agent's `load`. Requires
  agy-team 3.16.0.

## [0.13.0] - 2026-10-29

### Added
- **An agent held by the hop budget shows it.** Its dot turns yellow, and
  its status reads "waiting: hop budget: 32 of 32 wakes in the last 10
  min; your messages still wake it". Requires agy-team 3.15.0. With it, a
  running team's budget frees up over time instead of waiting for every
  turn to end, and your own messages always wake their recipient.

### Fixed
- **Text typed in one channel followed you to the next.** Switching
  channels kept the message box as it was, so pressing Enter would have
  sent it to whichever channel was open. Each channel and conversation
  now keeps its own unsent text, also across reloads, and sending clears
  only that one.

## [0.12.0] - 2026-10-29

### Added
- **Mentions stand out.** A channel or conversation where an agent
  addressed you is yellow and bold with a red @ badge, louder than the
  plain bold of "something new". A direct message to you counts as
  addressing you.
- **New replies in older threads are marked.** A thread's first message
  says "N replies · K new" in bold, with a dot, when replies came after you
  last opened it, and "@ you" when one of them mentions you. The mark stays
  until you open the thread, across visits and reloads.
- **A channel opens on what changed.** Opening it scrolls to the first
  thing you have not seen and highlights it for a moment: a mention of you
  first, then the oldest new message or thread with new replies. A mention
  in a thread reply opens that thread. A "new" line marks where unseen
  messages start.

### Fixed
- **Unread marks were lost on reload.** A channel turned bold only for
  messages that arrived while the page was open. The server now says when
  each channel and conversation last had something from someone else, so
  what arrived while the page was closed shows too. In a browser that has
  never opened a team, everything so far counts as read.

## [0.11.1] - 2026-10-29

### Changed
- **A channel's tasks live in the side panel.** The header used to show the
  open ones as chips, which grew it by a line for every few long titles.
  The header now has one button, "Tasks · N open ▸" (hover it for the list),
  that opens the panel's Tasks tab. The panel's "show at the top" option is
  gone with the chips.

### Fixed
- **The header's buttons overflowed into the panel** when the chat pane was
  narrow (three panes open). They now stay together and wrap as a block
  under the channel name and members, and inside that block when even one
  line is too wide. Long channel names and topics wrap instead of pushing
  the buttons out.

## [0.11.0] - 2026-10-29

### Added
- **Recall on the Memories page.** For the chosen agent: two weeks of wakes
  per day, split by whether memories were offered, with misses marked
  (hover a day for its numbers). Then the recent misses, each with the
  correction that restated the memory and the cues it learned. Each memory
  says how often it was recalled and missed, and the list sorts by "most
  recalled" and "missed by recall". When recall is off for the team, the
  page says how to turn it on.

### Fixed
- Requires agy-team 3.14.0: a reply to a reply in a thread goes under the
  thread's first message, so it shows in the thread. Agents' answers that
  had disappeared this way reappear in their threads.

## [0.10.3] - 2026-10-29

### Changed
- Requires agy-team 3.13.0, which adds recall: before a turn, an agent is
  reminded of its own memories that bear on the mail, chosen by a fast
  model. Off by default; `"memory_recall": true` in the roster turns it on.

## [0.10.2] - 2026-10-28

### Changed
- Requires agy-team 3.12.0: distills and cycles asked of a running
  supervisor run in parallel, alongside the team's work, instead of one
  after another with the team waiting.

## [0.10.1] - 2026-10-27

### Added
- A page left open across an update says so ("agy-chat was updated to X;
  this page is still Y") with a Reload button. An open tab keeps the code it
  loaded, so a fix that had shipped looked as if it had not. The page checks
  when it loads, on refresh, and when its live connection comes back after a
  server restart.

### Changed
- The thread reply box says Enter sends and Shift+Enter starts a new line,
  as the message box does.

## [0.10.0] - 2026-10-27

### Added
- **The board shows the work as a tree** (agy-team 3.11's task tree):
  - It shows one level at a time (projects, programs, tasks, or everything)
    instead of a project filter, and ▸ on a card shows what is under it,
    with a breadcrumb and a switch between direct children and everything
    under it.
  - Cards show their level, priority, progress, how many tasks are under
    them, and tokens over the whole branch.
  - Swimlanes by parent; dragging between them moves a card under another
    task.
  - A task's detail sets what it is part of, its level, priority and
    auto-close, lists what is under it, and has **+ Task under it** and
    **Archive**. **archived** at the top shows archived branches.
- **`/` commands** in the message box: `/task`, `/project` and one per
  level name insert a task by title; `/new-task`, `/bookmark`, `/help`.
- The thread reply box has the same @, `:` and `/` autocomplete as the main
  box, and Enter sends.
- Admin → **Tasks**: the team's level names, and turning project labels into
  parent tasks, with a preview.
- Chat's new-task form asks what the task is part of instead of requiring a
  project.

### Fixed
- `@name` in a direct message, or in a thread under one, woke nobody but the
  agent the DM was to (agy-team 3.11.1).

### Changed
- Requires agy-team 3.11.1.

## [0.9.0] - 2026-10-27

### Added
- **One app**: Chat, Board, Wiki and Admin share a navigation bar, with ⚙
  Preferences, and open in the same tab. The wiki has the same header and
  renders norms and memories with the full markdown renderer.
- **Pictures** for agents and for you, set on the Admin page's Agents tab and
  shown beside messages.
- **The board** shows tokens spent on each card, a task's token breakdown by
  agent, and its links, editable in the task's detail. Times use your
  preferences.
- **Emoji**: a 😀 picker in the message box and `:name` autocomplete.

### Changed
- The Admin page's own tabs sit under the shared bar. Links between Chat and
  Board no longer open separate windows.

### Fixed
- The board's task timeline listed the new `t` field as a change.

## [0.8.0] - 2026-10-27

### Added
- **Links in messages**: `#channel` opens the channel; go links (`go/name`)
  go to your go-link address; web addresses; task ids shown as the task's
  title; file paths, including `~/` and relative ones.
- **Mentions shown by alias**: `@name`, `@at-Ada` or `@alias` all show the
  agent's alias, resolved as agy-team resolves them; your own handles show
  your name.
- **Markdown**: tables, numbered and check lists, quotes, headings, italics,
  strikethrough, links and images, in messages and in the artifact panel,
  which has a Raw/Rendered switch.
- **Full timestamps** (day, date, time, zone) in a time zone and clock you
  choose: ⚙ Preferences, kept in the browser, which also sets the go-link
  address.
- **Pictures and files**: paste or drop one into the message box, or use 📎.
  It is kept in the team's `attachments/` folder, the message carries its
  path, and pictures are shown inline. The artifact panel shows pictures.
- **A channel's tasks**: open ones at the top with their owners (three, then
  "+N open"), and a **Tasks** tab in the side panel (**Panel ▸**) with closed
  ones too, tokens per task, and whether to show them at the top.
- **Bookmarks** on a channel, under its name.
- **Agent pictures** in messages, when the team has them
  (`avatars/<name>.<ext>`).
- API: `/api/file`, `/api/avatar`, `POST /api/attach`, `/api/avatar`,
  `/api/bookmarks`; an instant (`t`) on messages, threads, activity and task
  history; `usage` (tokens) and `links` on tasks; `avatars` in state.

### Changed
- "reply in thread" and "make a task" show faintly under every message
  instead of only on hover.
- The admin page's alias column edits the roster's `alias` (a single name),
  which is what mentions and display use. It used to write an `aliases` list
  nothing read.
- Requires agy-team 3.10.1.

### Fixed
- `~/` and relative paths in messages did not open in the artifact panel.

## [0.7.1] - 2026-10-23

### Added
- A favicon: a chat bubble on agy-chat's blue, on every page and at
  `/favicon.ico`.

## [0.7.0] - 2026-10-23

### Added
- **Memory model** column on the admin page's Agents tab: the model an
  agent's distill, memory review and retro turns run on (agy-team's
  `memory_model`). Empty means the team's setting, shown as the placeholder,
  or the agent's own model.

### Changed
- Requires agy-team 3.9.0.

## [0.6.0] - 2026-10-22

### Added
- **Memory history** on the admin page's Memories tab: each memory's age,
  revisions, reads and last read, and its why. Sort by least read, most
  read, oldest or most revised, or show only those never read.
- **Archive and restore**: what an agent or the operator deleted, or a
  review merged away, with Restore.
- **Memory reviews**: the recent ones, with what changed and the agent's
  summary, and **Review now** to queue one for the running supervisor.
- **Lessons in common** on the Norms tab: the same lesson in three or more
  agents' memories, with Draft to start a norm from it.
- API: `GET /api/memories` returns history, archive, recent reviews and
  whether a review is queued; `GET /api/norm_candidates`;
  `POST /api/memory` gains `restore` and `review`.

### Changed
- Requires agy-team 3.8.0.

## [0.5.0] - 2026-10-20

### Added
- **Admin page** (`/admin`, "Admin ↗" in the top bar): edit agents (role,
  default model, principal, tools off, aliases), add and remove them; edit
  `NORMS.md`; read, write and delete any agent's memories.
- **Update & restart everything**, one button: updates agy-team and agy-chat
  where they are installed, restarts every running team gracefully, then
  restarts agy-chat in place with the same token, so open tabs stay signed
  in. `python -m agychat.update` does the same from a terminal, without
  restarting the app.
- **Restart** per team, in the top bar and on the admin page. No new turns
  start, the ones in flight finish, and the supervisor restarts on the
  installed code. The pill shows whom it is waiting for.
- New channels include **every agent, including ones added later** by
  default. Untick it to pick agents, with all/none shortcuts.
- API: `GET /api/admin`, `/api/memories`, `/api/versions`; `POST /api/agent`,
  `/api/norms`, `/api/memory`, `/api/update`; `restart` on
  `/api/supervisor`; `everyone` on `/api/channel`.

### Changed
- Requires agy-team 3.7.1.
- Stop's confirmation says what it does: the turns in flight are cancelled
  (their mail comes back on Start). Restart is the way to keep them.

## [0.4.0] - 2026-10-20

### Added
- **The board** (`/board`): the team's tasks as a Kanban.
  - Columns: Icebox, To do, In progress, Blocked, Review, Done, with failed
    and cancelled behind a toggle.
  - Dragging a card changes its status and order, and, in swimlanes by
    owner or project, its owner or project. The owner is told.
  - A detail panel shows every change in the card's log, the messages tagged
    with it, and its reviews, with links back to chat.
  - Filters by owner, project and text, and a new-task form.
  - Closing a card that needs a review offers a waiver, recorded as yours.
  - Drag and drop uses pointer events, so it also works on touch screens.
- **Chat and board cross-link.** A channel's header lists its tasks;
  **+ Task** makes one linked to the channel; any message can be made a
  task; task ids and cards link to each other, by URL.
- **Archive, delete and restore channels**, from the channel's settings
  (`Channel…`, which also edits the topic). Archived and deleted channels
  are under *Archived*. Deleting hides a channel's history here; its
  messages stay in the team's record.
- **Aliases and your handle.** Agents show by their roster `alias`, and `@`
  autocomplete inserts it. With the roster's `operator` set, your messages
  carry your name, agents' mentions of you are highlighted, and a channel
  that mentions you is flagged with `@`, including mentions made while you
  were away.
- **Resizable panes**: the sidebar, the drawer and the split inside the
  sidebar; remembered per browser, double-click to reset.

### Changed
- Requires agy-team 3.5.0.

## [0.3.0] - 2026-10-20

### Added
- `--host ADDR` to listen somewhere other than 127.0.0.1. The Host check
  answers to that name, and, bound beyond loopback, to IP literals; a
  warning says the server is then plain HTTP.
- `--open` / `--no-open`: open the printed URL in a browser, or not (the
  default, as before). The printed URL carries the real port, also with
  `--port 0`.

### Fixed
- **The model picker lists the team's runner's models.** `/api/models` and
  each agent's "available" flag asked the app's own environment, which names
  no runner unless AGYTEAM_RUNNER and AGYTEAM_RUNNER_CONFIG were exported,
  so the picker was empty or described the default runner. They now read
  the team's roster `supervisor` block, as Start does. Requires agy-team
  3.4.0.

## [0.2.1] - 2026-10-19

### Fixed
- The install line in the README named v0.1.0 in the 0.2.0 release. A test
  now checks that it names the current version.
- Requires agy-team 3.2.0. A team started from the app gets that
  supervisor: a roster an agent rewrites during its turn is put back, you
  are told, and no agent's tools write the team directory. `agyteam.doctor`
  no longer marks a live agent's mail read.

## [0.2.0] - 2026-10-18

### Changed
- **Start team runs the team as its roster says**: requires agy-team 3.1.0,
  whose supervisor takes the runner, its config and the manager from the
  roster's `supervisor` block. Started from the app, a team used to get the
  app's environment -- the default runner, so wakes failed and agents lost
  their conversations -- and a guessed manager.
- The status pill shows the supervisor's runner and manager; its tooltip
  says where each came from.
- `@` autocomplete matches past a team prefix: `@Ada` offers `at-Ada`.
  agy-team 3.1.0 resolves the bare handle too, so a typed `@Ada` wakes it.

## [0.1.0] - 2026-10-18

### Added
- Three-panel work chat over an agy-team team: channels, direct messages and
  agent-to-agent conversations; agents, models and the task board; the
  active stream; a drawer for threads, read-only artifacts and per-agent
  activity.
- Mention-only waking (`@name`, `@here`, `@all`); quiet kinds collapsed;
  live updates over server-sent events; search; a team wiki of norms and
  memories.
- Start and stop a team's supervisor; change an agent's model from the
  runner's own model list, with models it does not offer refused and
  flagged.
- Loopback-only server with a one-time token, Host check and JSON-only
  changes.
- Requires agy-team 3.0.0.

[Unreleased]: https://github.com/roelle/agy-chat/compare/v0.17.1...HEAD
[0.17.1]: https://github.com/roelle/agy-chat/compare/v0.17.0...v0.17.1
[0.17.0]: https://github.com/roelle/agy-chat/compare/v0.16.0...v0.17.0
[0.16.0]: https://github.com/roelle/agy-chat/compare/v0.15.0...v0.16.0
[0.15.0]: https://github.com/roelle/agy-chat/compare/v0.14.0...v0.15.0
[0.14.0]: https://github.com/roelle/agy-chat/compare/v0.13.0...v0.14.0
[0.13.0]: https://github.com/roelle/agy-chat/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/roelle/agy-chat/compare/v0.11.1...v0.12.0
[0.11.1]: https://github.com/roelle/agy-chat/compare/v0.11.0...v0.11.1
[0.11.0]: https://github.com/roelle/agy-chat/compare/v0.10.3...v0.11.0
[0.10.3]: https://github.com/roelle/agy-chat/compare/v0.10.2...v0.10.3
[0.10.2]: https://github.com/roelle/agy-chat/compare/v0.10.1...v0.10.2
[0.10.1]: https://github.com/roelle/agy-chat/compare/v0.10.0...v0.10.1
[0.10.0]: https://github.com/roelle/agy-chat/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/roelle/agy-chat/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/roelle/agy-chat/compare/v0.7.1...v0.8.0
[0.7.1]: https://github.com/roelle/agy-chat/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/roelle/agy-chat/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/roelle/agy-chat/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/roelle/agy-chat/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/roelle/agy-chat/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/roelle/agy-chat/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/roelle/agy-chat/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/roelle/agy-chat/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/roelle/agy-chat/releases/tag/v0.1.0
