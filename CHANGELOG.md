# Changelog

All notable changes to agy-team. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) from 3.0.0 on; see "Versioning" in
the README for what counts as the public surface.

## [Unreleased]

## [4.18.0] - 2026-11-12

### Added
- **Learned triggers, judged by what happened after.** Which memory
  comes up before which call is now a record that each turn corrects
  (`ateam/trigger_learn.py`). When a call goes wrong and is made again
  differently, with no file changed in between, the memory that would
  have made the first try go right gains a trigger for such calls: the
  one the agent opened before the try that worked, or else the one a
  judge names from the call, what came back, what worked and the
  agent's memories. When a memory is shown before a call, the judge says
  whether it bore on it. A trigger whose recent record falls below
  `show_above` (0.3) stops being shown, whoever wrote it: a learned one
  is retired, a written one is muted with the memory untouched and back
  in wake recall, and the same evidence brings either back. Each verdict
  keeps `decay` (0.9) of the old counts. A memory that was out of sight
  when its call went wrong is shown again at the next one. Saving a
  memory with `triggers` set clears what was learned for it.
  `memory_index` marks `[learned]` and `[not shown: ...]`. The judge is
  recall's, or `"memory_triggers": {"judge": ...}`; without one, only a
  memory opened after a failed call and a held call that came back
  changed count, and nothing is muted. New events `trigger_learned`,
  `trigger_muted`, `trigger_verdict`, `trigger_trouble`;
  `python -m ateam.trigger_learn [agent]` prints the record.
  `"memory_triggers": {"learn": false}` turns it off.
- **Lessons that are missing are asked for.** Calls that went wrong and
  were made again with no memory to help (and calls the agent looked up
  with `--help` first) are listed in the agent's next distillation
  prompt and memory review: the first try, what came back, the number
  of tries and what worked, with the trigger to save the lesson under.
  A memory that was shown before a call that went wrong anyway is named
  as one to deepen.

### Changed
- A trigger matches a command behind `sudo`, `env`, `time`, `nohup` or
  `VAR=value`, and by the program's name wherever it is run from:
  `Bash(kubectl *)` now matches `sudo kubectl apply` and
  `/usr/local/bin/kubectl apply`.
- The `triggered` event carries `via` (the trigger each memory came up
  by), `call` and `family`.

## [4.17.0] - 2026-11-11

### Added
- **Memory triggers.** A memory can name the calls it is about, and it
  then comes up before those calls instead of on every wake.
  `save_memory` and `merge_memories` take `triggers`, written like
  Claude Code permission rules and separated by `;`: `Bash(kubectl *)`,
  `Bash(git push)`, `send_to_teammate(user)`, `create_task(web*)`,
  `Edit(*.ipynb)`, `mcp__github__*`. The first matching call is held
  and the agent gets what it has on that call, with a request to make
  the call again; the second call goes through. A NORMS.md section with
  a `Triggers:` line in its first lines does the same for every agent.
  Shown again after a new conversation, a compaction, `rearm_tokens`
  (100,000) of context growth or `rearm_hours` (6), and when the memory
  or norm changed. Held in the Claude runner, the Antigravity SDK
  runner, OpenCode over ACP, any host running
  `python -m ateam.hook_pre_tool_use` and the team's own tools on every
  host; where a call cannot be stopped (agy's and Codex's own tools) it
  is a note at the next step. Each showing is a `triggered` event.
  Roster: `"memory_triggers": false` turns it off; an object overrides
  `rearm_tokens`, `rearm_hours` and `max_chars` (6000).
- **Browser Back and Forward in the chat app.** Each channel or
  conversation, thread and message you open is a history entry, so
  Back and Forward (and Alt+Left, a mouse's back button) walk them.
  Going back to a view without a thread closes the thread panel; a
  message link you followed goes back to where you were.

## [4.16.0] - 2026-11-11

### Added
- **`task_projects_limit`.** `allowed_task_projects` held for everything an
  agent does with tasks, `list_tasks` included, so an agent that coordinates
  across projects could not see the tasks it was meant to follow, or one a
  teammate asked it to pick up. `"task_projects_limit": "create_assign"` in
  the agent's roster entry, or team-wide under `"policy"` (the entry wins),
  keeps the limit to creating and assigning: `create_task` in or under a
  project outside them, and `update_task` giving a task outside them an
  owner or moving it under a parent outside them, are refused. Listing
  (`under` included), claiming, updating notes or status and completing any
  task are open. `"all"`, or no key, is unchanged: the limit holds for
  everything. `"create-assign"` and `"assign"` are accepted too.

### Fixed
- **Heartbeat while the supervisor waits on a turn of its own.** The
  supervisor writes `<team_dir>/supervisor.json` from its dispatch pass, and
  it is stale after 2 minutes. A distill (auto-cycle's, or the nightly
  sweep), a memory review or a retro waits on its turn in the supervisor's
  own thread, so the pass did not run, the heartbeat went stale, the app
  said "no supervisor: nothing will wake" and a watcher reading the
  heartbeat paged the operator. Now a background thread rewrites the
  heartbeat every 15 s during such a wait, and a new field `busy` says what
  it waits on (`distilling coder`, `reviewing coder's memories`, `retro: qa
  reflecting`), cleared when the wait ends; the app's status pill shows it
  ("running · claude · distilling coder"). A one-off `--distill` run that
  does not drive the team writes no heartbeat, so it never passes for the
  team's supervisor.
- **The "skipping auto-cycle: work outstanding" log line.** Auto-cycle runs
  on every idle pass, about once a second, so an agent over its context
  threshold with mail pending logged that line every second: some 20 MB of
  log a day. It also counted quiet mail (ack, fyi, status, or a channel post
  that does not mention the agent) as work outstanding; quiet mail does not
  wake an agent, so it could hold the cycle off for days. Now only mail that
  will wake the agent holds off the cycle (quiet mail waits for the next
  wake, which a reset does not lose), the line reads "not cycling coder
  yet: N message(s) waiting to wake it; it cycles once they are handled"
  and is said once per agent, then at most every 10 minutes while it lasts.
  Inboxes are peeked once per pass instead of once per agent.
- **A full disk killed the daemon.** The supervisor's log is its stdout
  redirected to a file. When the disk filled briefly, the next print raised
  `OSError` (ENOSPC) out of the dispatch loop and the daemon exited; the
  disk came back, the team did not. Now `--daemon` wraps stdout and stderr:
  a write that fails is dropped and counted, and the first write that works
  again is preceded by "[supervisor] N log line(s) lost: OSError: ...".
  This covers prints from runners in the same process too, and `_log`
  itself swallows the error.
- **A turn that timed out after repeated 503s is a capacity failure.** A
  host CLI that retries a 503 inside the turn can spend the turn's whole
  clock being refused. The turn ended as "[error: the turn ran past 600s]",
  which reads as a slow agent, so `fallback_models` never kicked in and the
  next turn went to the same model. Now a turn that ran out its time after
  2 or more capacity refusals says so ("[error: the turn ran past 600s
  after the provider refused it 3 times for capacity (last: 503 ...)]"),
  the fallback reads it as capacity, the model is cooled down and the next
  attempt runs on the next fallback model. The Claude runner sees the
  refusals in the CLI's `api_retry` notices, Codex in app-server `error`
  notifications with `willRetry` and its stderr, ACP in the harness's
  stderr, agy in what it wrote to stderr before the timeout; the host
  runner cannot see inside the turn, so its timeouts are unchanged. With no
  fallback named, such a timeout is not retried on the same model, as a
  plain timeout never was. One refusal, or a timeout with none, is still a
  plain timeout.

## [4.15.0] - 2026-11-10

### Added
- **A menu on every message.** Right-click a message, in a channel, a direct
  message or a thread, or click its ⋯ (shown on hover beside "make a task",
  always shown on a touch screen). It offers Copy link to message; Copy link
  to thread, when the message is in or has one; Reply in thread, or Open
  thread; Mark unread from here; Follow up later, or Done following up
  while one is open for that message; Make a task…; Copy text; and Edit and
  Delete… on your own messages. A right-click on a link, a picture or a
  text box, with text selected, or with Shift held, gives the browser's own
  menu.
- **Links to messages and threads, pasteable anywhere.** A message's link
  is `/?team=T#channel=C&msg=ID` (a reply adds `&thread=ROOT`; a direct
  message is `#dm=coder`, and a conversation between two agents `#dm=a,b`);
  a thread's is `#channel=C&thread=ROOT`. Opening one opens the view, opens
  the thread if the message is a reply, scrolls to the message and marks it
  for a moment; a message older than the 500 the view loads opens in the
  side panel. A link works pasted anywhere, in the browser that holds the
  app's cookie. Pasted into a message, a link into this app is shown as a
  short in-app link named for what it points at: `↪ coder: "first words…"`
  for a message (looked up if it is not loaded), `↪ a thread in #billing`,
  `↪ #billing`; `[text](link)` keeps your text. Clicking one stays in the
  tab, and a link to another team loads that team. The server answers
  `GET /api/messages?id=ID` with the message.
- **Mark unread.** From a message's menu, what you have seen is set to just
  before it: in a thread, the thread's (the drawer closes, and its first
  message says "N new" again); otherwise the view's, so the **new** line
  shows above that message and the channel or conversation stays bold in
  the sidebar until you next open it, even if it is open now. Right-click a
  channel or conversation in the sidebar for Mark unread, Mark read (its
  threads too) and Copy link. Like the rest of what you have seen, this is
  kept in this browser.
- **Follow up later: a list to get back to, made of tasks you own.** From a
  message's menu, it makes a task owned by you (owner `user`), titled
  "Follow up: <first line>", with the message as its origin and a link to
  it in the note, and no channel, so nothing is posted. It shows in Waiting
  on you as "Your task", with 💬 to open the message and a ✓ that closes it
  (marks it done); a 🔖 tag under the message shows it while it is open, and
  "Done following up" in the menu closes it too. Only the operator has it;
  people with links of their own do not.
- **Any message can make tasks, as many as you like.** Every message, a
  reply in a thread included, has "make a task", and a message can make any
  number of them; before, "make a task" went away once a message had
  replies, and a thread reply had none. Each task made from a message is
  listed under it as a tag with its status dot and title, linking to the
  board. The new-task form's Owner list has you ("<your name> (you: it waits
  on you)"). A task's origin link on the board, and the 💬 on its Waiting on
  you row, now open the exact message, in its thread when it is a reply,
  not just the thread.
- **No message from you to you.** A task created for or handed to the
  operator (owner `user`) no longer sends the operator a message about it;
  before, the app's "tell the owner" wrote a message from the operator to
  the operator.

### Fixed
- **A reply no longer shows in the main channel list, then moves back under
  its thread on a reload.** Three causes. The live feed sent a reply to a
  reply as written, naming that reply rather than the thread's first
  message; on a reload the record resolves it, so the page grouped the
  reply differently live. The feed now resolves it as a reload does (agents
  on current code already write the first message; older agent code did
  not). A message arriving while another view was still loading went into
  the list of the view being left, where its thread's first message was
  not, so it showed as a post of its own until the view finished loading;
  the page now adds live messages only to the list loaded for that view.
  And a view loads its last 500 messages, so a reply among them whose
  thread began earlier showed as a post of its own; the view now brings in
  the first message of each thread it shows. Six tests in
  `evals/chat/test_app_links_and_followups.py` cover the links, follow-ups
  and these fixes.

## [4.14.0] - 2026-11-09

### Added
- **Standing duties: a task with `every` repeats on a schedule the sweep
  keeps.** Some work never finishes: sweeping the user's email and chat,
  watching a dashboard where push is not possible. It had no home. A
  reminder fires once and the agent re-arms it, so a failed turn, an agent
  that closed the task when a pass was done, or a supervisor that was down
  when the reminder came due each ended the duty, silently: no record said
  it should have run, and nobody heard about the miss. Now a task with
  `every` is a standing duty. `every` is a duration (`30m`, `2h`, `1d`;
  under 5 minutes is refused as a loop, use `until` to wait for something)
  or a time of day (`daily 08:00`, `weekdays 17:30`, `weekends 10:00`,
  `mon,thu 09:00`), with an IANA zone; given without one it takes the
  host's zone and stores it (`weekdays 08:00 America/Los_Angeles`). A
  schedule that cannot be kept is refused with a sentence when set. A duty
  needs an owner: an agent that creates one owns it by default, the
  operator picks one. A duration duty runs at once; a time-of-day duty at
  its first time. The sweep (the supervisor on every pass, or `python -m
  ateam.tasks sweep`) delivers each run to the owner as a reminder
  message with the task id and sets the next run from the schedule itself,
  whatever became of the last; the agent never re-arms anything.
  `complete_task` on a duty records the run (the note is what the run
  found, kept as `last_note` with `last_done`) and leaves the duty open:
  "[run recorded: … is a standing duty and stays open; its next run is
  due …]". An agent's `update_task(status=done/failed/cancelled)` on a
  duty is refused with how to record a run instead. The operator ends a
  duty (done or cancelled from the board), or `every=''` turns it back
  into an ordinary task. The operator's create path tells the owner
  quietly (fyi) that they have a duty; the first run comes as the reminder.
- **Missed runs are recorded and reported, never silent.** A run is
  missed when it was due and never marked done by the time the next came
  due: the next reminder tells the owner "your last run, due …, was never
  marked done". Runs that fell due while nothing swept (team stopped, host
  down) are counted too, and caught up with one run, not one per miss: the
  reminder says how many and between when, and asks the run to cover what
  they would have. Each miss is on the task (`missed` in a row,
  `missed_total`, `last_missed`) and in its timeline. A failed turn does
  not end the duty. A duty linked to a channel gets a quiet notice in its
  thread when runs are missed. **Waiting on you** shows "<owner>'s duty is
  behind" when a duty has missed two runs in a row, or when its next run
  is overdue by more than a period (at least 15 minutes) with nothing
  delivering it; that is read from the task alone, so it shows even when
  nothing is sweeping. One miss is the owner's to handle.
- **The board has a Standing column**, shown once the team has a duty,
  holding open duties. Each card shows 🔁 and the schedule, and a red
  "N missed" tag. The detail panel has a **Repeats** field (set or clear
  the schedule) and a line with the next run, the run out with its owner,
  last done and its note, and misses in a row and in all; the timeline
  lists each run sent and each run done. Dropping a card on Standing asks
  for a schedule; dropping a duty elsewhere asks whether to stop it
  repeating. The new-task form has Repeats. Agent tools:
  `create_task(every=...)`, `update_task(every=...)` (`''` to clear);
  `list_tasks` shows "standing duty, every X; next run due …; last done …;
  N missed in a row". New module `ateam/duties.py`.

## [4.13.0] - 2026-11-09

### Fixed
- **The app no longer slows down as the history grows.** Every request
  re-read and re-parsed the whole message log, six times for one state
  refresh, and the 4.12.0 mention check ran a regex over every message's
  text on every refresh; on a 16 MB, 11,000-message team a channel took a
  quarter of a second and got slower with every message. The log is now
  parsed once per app process and then only what was appended (a line
  still being written waits for its end; a log rewritten in place or
  replaced is read from the start again). What is derived from it, the
  merged edits, thread roots, reply counts, conversations, the per-view
  "what's new" and the mentions of you, is worked out again only when the
  log or the edits change, and whether a message mentions you is
  remembered per message. Measured on that team: a channel view 105 ms →
  2 ms in-process (9-12 ms over HTTP in the browser), a state refresh
  350 ms → 3 ms, and 14 ms right after a new message.
- **A channel stays bold while a thread in it has unread replies.** Opening
  a channel marked it read even when a thread in it had replies you had
  not opened, so a reply in a thread you were not looking at went unseen.
  Now a channel or conversation stays bold while any thread in it has a
  reply since you last opened that thread; a thread you never opened
  counts from when you last looked at the channel. Your own replies and
  quiet updates (ack, fyi, status, task notices for one) do not count. The
  server's state carries `thread_latest`: per view, the 50 most recently
  replied threads and when each last had a reply worth reading.
- **A long code line no longer widens the whole message.** A message's
  column could not shrink below its widest line, so one long line of code
  pushed every paragraph in the message past the pane, and you scrolled
  sideways to read prose. Worst in the narrow thread pane. A message now
  keeps to its pane: prose wraps to it, and code blocks wrap too (nothing
  hangs past a pane's edge, since that is what made the pane scroll
  sideways). The copy button still copies the code as written.

## [4.12.0] - 2026-11-06

### Added
- **The page says when the server is behind the code on disk.** The page is
  read from disk on every load, but the server runs the Python it started
  with. An upgrade under a running server (a pip or pipx upgrade, or a pull,
  without restarting a-team) gives a new page talking to an old server: its
  delete button, for one, called an endpoint the server did not have, and the
  page showed a red "unknown endpoint". The server now reports the version
  installed on disk (`installed_version`, read fresh) next to the one it runs
  (`app_version`). When they differ the page shows a banner, "a-team X is
  installed, but this server is still running Y", with a **Restart a-team**
  button. It restarts a-team in place on the installed code, same port and
  token; the page reloads when it is back, and people with their own link
  are told to ask the operator. If the server answers "unknown endpoint",
  the page says a-team was upgraded but this server still runs the old code,
  restart it to finish the update, and shows the same banner. New action:
  `POST /api/update {"action": "restart"}`. A server older than 4.12.0 has no
  restart action, so the button cannot restart it: restart a-team by hand or
  use Admin's Update & restart.

### Fixed
- **The red @ on a channel, not only on a DM.** It came only from the
  `mentions` an agent recorded on its post. Agents see your name on what you
  post (the app posts as `--author`, by default `$USER`) and write @matt;
  that was recorded as a mention only if the roster's `operator` listed that
  handle, and agents on older code never recorded @user. So a direct message
  got the red @ and a channel mostly did not. The app now reads the @ itself
  when it reads the post, in the server's per-view mentions of you and in
  the page: @user, the roster operator's name and handles, and the name you
  post as, in any case. An email address (matt@example.com) is not a
  mention. The red @ shows on the channel live and after a reload. The
  server's state carries `my_handles`.

## [4.11.0] - 2026-11-05

### Added
- **A task links back to the discussion it came from.** A task has `thread`
  (the id of the message in its `channel` that its discussion hangs off) and
  `origin` (the message it was made from: id, channel, thread, from, to; a
  direct message included). An agent that calls `create_task` while answering
  a channel post gets the task linked to that channel and that post's thread
  without naming either; a channel it names wins, and `origin` is recorded
  either way. A linked task with no thread starts one with its first notice in
  the channel ("Task X: created"), and every later notice about it is a reply
  in that thread, not a new top-level line; a channel change starts over in
  the new channel, and unlinking clears the thread. A task set to `blocked`
  with no channel is linked to the conversation the agent is in; with none,
  the tool reply says it was blocked with no channel and names the channels
  to pick from. `create_task` and `update_task` take `thread`. In the app, a
  card's `#channel` shows 🧵 and opens the thread, its detail says where it
  came from ("from #channel", or "from A → B" for a DM) and offers "Open its
  thread in #channel", "Waiting on you" rows have a 💬 that opens the
  discussion, and a task made from a message in chat is linked to that
  message's thread. `/?team=T#channel=C&thread=ID` opens a thread. The
  board's timeline shows when the thread started.

## [4.10.0] - 2026-11-04

### Added
- **Agents on OpenCode, and on any harness that speaks ACP.**
  `ateam.runner_acp:AcpRunner` drives an agent harness over the Agent Client
  Protocol, the one editors use: each turn resumes the agent's session, sets
  its model, hands it the team's MCP servers, sends the wake, and records the
  tool calls, tokens, context size and cost the harness reports. OpenCode is
  the default (`{"agent": "opencode"}`), and agents run on whatever providers
  and sign-ins it already has; Grok Build, Goose and Gemini CLI are presets by
  their documented ACP commands, not yet run; any other ACP agent goes by
  `"command"`. Permission requests are answered from the team's policy, and
  for OpenCode a plugin of a-team's checks every tool call with its full
  arguments and keeps provider keys out of the agent's shell. Run for real:
  a two-agent OpenCode team (Gemini) wrote a file, asked for a review over the
  bus, and the reviewer ran it and recorded an approval; a read of a
  `refuse_paths` file was refused with the reason. The first-run page offers
  OpenCode when the `opencode` command is installed.
- **The README says what a-team is**: not an agent, but a team of durable
  agents running in one agent harness or many, with the durability and the
  teamwork a single harness does not have.
- **Distilling shows as its own state.** An agent saving what it learned to
  memory, asked for or before an auto-cycle, has a blue pulsing dot and says
  so, where it showed as working (or, for auto-cycle's distill, idle).
  `Record.presence()` gives it as `"distilling"`.

## [4.9.0] - 2026-11-04

### Added
- **Task projects per agent.** `allowed_task_projects` in an agent's roster
  entry (names or patterns, `web-*`) keeps it to those projects: it creates,
  claims, changes, closes and lists tasks only there. With one project and
  none named, a new task goes in it. Enforced in the task server, so it holds
  on any host.
- **A subtask is in its parent's project.** Created under a task, it takes
  that task's project, and naming another is an error; moved under another
  task, it takes that one's. `update_task` takes `project`, which moves the
  task and everything under it that was in the same project. So scoping a
  project scopes its whole tree.
- **`write_roots`**, the allow-list beside `refuse_paths`: per agent or
  team-wide under `policy`, the only places an agent may write (with its own
  workspace and the shared directory). A file tool that writes, and a shell
  command's redirections, copy destinations, `rm`/`touch`/`tee`/`sed -i`
  targets and the like, are checked. Reads are not limited.
- **`refuse_paths` on a shell command's words.** The command is split as
  the shell would split it (quotes keep a word together, `--out=PATH` opened
  up, `sh -c` split again), each path resolved against the call's working
  directory, and a command run from inside a refused directory is refused.
  `cat key.json` from inside one, or `cat ../answers/key.json`, got past the
  text match.
- **An image still being written is not read.** A file tool reading a
  .png, .jpg, .gif or .webp without its end marker is refused with a note to
  wait. Read mid-write, a truncated image reaches the model, the request is
  rejected, and on some runtimes so is every later one in that conversation.
  A JPEG unchanged for ten minutes passes (data after its end is legal).
  `"policy": {"refuse_partial_images": false}` turns it off.
- **`operator_reminder`** in the roster: a line added to every wake that
  carries a message from you, for standing instructions that otherwise live
  in a brief read once.
- **`memory_nudge_tokens`** in the roster: once an agent's conversation
  passes that size, its next wake asks it, once per conversation, to save
  what it has learned before it is compacted or cycled. Both can be set per
  agent, and "" or 0 there turns them off for that one.
- **Falling back when a model is full.** With `fallback_models` in the
  roster (and optionally `fallback_cooldown`, 600 s by default), a turn
  refused with a 429 or a 503 cools that model down for the team, and its
  agents run on the first fallback that is not cooling until it is over,
  instead of waiting to retry the same model. The Claude and Codex runners
  switch inside the turn's retry; for agy and host runners the supervisor
  switches on the failed turn. The model badge says "cooling". Without
  `fallback_models` nothing changes.
- **A copy button on code blocks** in the app.
- **Edit and delete what you sent.** Under your own messages in the app,
  and `actions.edit_message` / `actions.delete_message`. A copy an agent has
  not read yet is changed (or removed) where it waits; an agent that already
  read it, or is reading it now, gets a note with the new text, or to
  disregard it, in the same thread. `bus.jsonl` keeps the message as sent,
  `edits.jsonl` records the change and who was told, and the app shows the
  message as it now stands, marked edited or deleted. An agent's messages
  cannot be changed.
- **`launch` in the roster's `supervisor` block**: the command the app's
  Start button runs for the team instead of `python -m ateam.supervisor
  --daemon`, for a host whose supervisor needs its own interpreter or
  wrapper. `{team_dir}`, `{team}` and `{python}` are filled in.

## [4.8.1] - 2026-11-03

### Fixed
- **@user in a channel is a mention of you.** Agents are told to address
  'user', so in a channel they write @user. Only the roster `operator`'s own
  handles were recorded as a mention of the operator, so on a roster without
  one (or a post that used @user anyway) the app showed the channel as unread
  but never with the red @. @user now always counts; it still wakes nobody,
  and the brief tells agents it shows you the post.

## [4.8.0] - 2026-11-03

### Fixed
- **Notices from before messages had ids can be cleared.** Clearing went by a
  message's id, and notices written before ids had none, so neither the × nor
  Clear all could take them off Waiting on you. Each question and blocker row
  of `Record.needs_you()` now carries a `key`: its id, or for a message
  without one, a key made from the message itself, the same on every read.
  `actions.clear_waiting` and the app go by keys. What was cleared before
  still is.

### Added
- **How much quota a model has left, and what it costs.** A runner's model
  listing may carry `remaining` (0-1, of the tightest limit that applies),
  `resets_at`, `window`, `status`, `credits` and `cost` (USD per million
  tokens, from the pricing table), each where known. The Claude runner
  records the CLI's rate-limit events (a subscription's five-hour and weekly
  limits); the Codex runner the app-server's account limits and credit
  balance. A host's adapter reports its own with `Runner.note_quota`, or on
  its config's `models` entries. Kept in `quota.json`; a limit past its reset
  is dropped. `runner.list_models(team_dir=...)`, `ateam.models available`
  and the app's model picker show them.
- **A badge on an agent whose model is low**: under the roster's `quota_low`
  (0.2 unless set) left, or refused. `Record.models()` gives each agent's
  `quota`, with `low`.
- `key` on question and blocker rows of `Record.needs_you()`.

## [4.7.0] - 2026-11-03

### Added
- **A fast path for recall, in shadow.** A scorer that ranks every memory with
  no model call (`ateam/recall_fast.py`), trained by the judge on every wake
  and by what followed: a memory the agent opened that recall had not
  offered, the used-check's verdict, and corrections that restate a memory.
  Each memory keeps its own bounded evidence; six shared weights take smaller
  steps as they learn and are pulled back toward where they started. On by
  default when recall is on, as `"fast": "shadow"`: it records what it would
  have offered (`fast` on each `recall` event) and offers nothing; `"fast":
  "off"` turns it off. Agents still see only the judge's choice.
- The judge also grades up to two memories the fast path adds to its
  candidates (`fast_explore`, default 2), so the fast path learns beyond what
  it already ranks high. The judge can now find a memory the candidates
  missed.
- `bench/recall/fast_report.py`: how often the fast path agrees with the judge,
  by day, from a team's record; `--replay` reruns it from nothing over the
  recorded requests, with no model calls.
- Each `recall` event carries its request (`query`, the first 2000
  characters), an id (`rid`), and every candidate rather than the first ten.
  `recall_miss`, `recall_ignored` and `recall_used` name the recall they are
  about (`recall`).

### Fixed
- `bench/recall/late_misses.py` joins a miss to the turn it corrects by that
  id: since 4.6.0 a miss can be logged after the next wake's recall.

## [4.6.0] - 2026-11-03

### Changed
- **Recall no longer makes a turn wait for three model calls.** Before a turn,
  recall wrote cues for memories saved since last time, checked the mail for a
  miss, and asked the judge, one after another, while the turn waited; with
  the agy CLI judge that was 15-50 seconds. Now cues, vectors and the miss
  check run in the background, after each turn and beside each wake, and only
  the judge is on the turn's path.
- **A turn waits for the judge at most 5 seconds** (`"wait"` under
  `memory_recall`; it was `2 * timeout + 10`, 50 seconds by default). A slower
  judge's choice reaches the agent at its next step, like mid-turn mail, and is
  recorded as a `recall_late` event, with `delivered` false if the turn had
  already ended. `"wait": 0` never waits.
- A memory saved since the index was last refreshed is found by its words
  until its vector is written; before, with embeddings on, it was not found.

### Fixed
- **A reply arriving in a thread no longer clears what you are typing in it.**
  Each new message in an open thread rebuilt the whole panel, reply box
  included, and the draft was lost. Now only the messages are redrawn: the
  box keeps its text, cursor and focus. A thread reply's draft is also kept
  per thread in the browser, so another thread or a reload does not lose it.
- `recall_ignored` was never recorded: the check of whether an offered memory
  was used took the offer off the record when the turn ended, so a later
  correction of it looked like a memory never shown.
- The used-memory check and the second review run in a background pool of
  their own, so a 3-minute second review can no longer hold a turn's judge.

## [4.5.0] - 2026-11-02

### Added
- **A second review from another vendor's model on approved code**
  (`"second_review": {"model": "gpt-6-astra"}` in roster.json, or under
  `"policy"`; off unless the team asks). A reviewer on the same model as the
  author shares its blind spots. After a turn records an approved code review
  (one with a proof file), one call to OpenAI's Responses API (`store: false`;
  key from `OPENAI_API_KEY` in the supervisor's environment only; `"provider"`
  for another endpoint that speaks it, https or loopback) is given the review
  and what git says moved in each workspace since the episode began, committed
  or not, plus new files, up to 60,000 characters (`max_chars`), and asked for
  defects. `"paths"` limits it to changes touching those globs. It is findings
  only: it cannot block, approve or reopen. Findings go to the reviewer who
  approved, as a message from the supervisor, to check against the code and
  reopen the work if one holds; "No findings." sends nothing. Every check,
  skip and error is a `second_review` event, with tokens. It runs off the
  supervisor's thread, so it holds up neither the team nor the answer to the
  user: if the lead reports straight after the approval, the findings arrive
  after the report. Without git nothing is sent. 8 tests against a local
  stand-in; live on `gpt-6-astra` it found both planted bugs in a password
  check a review had approved, in 8 seconds and about 700 tokens. See the
  README for the rest.
- The image is built and checked on Docker Engine on every push and tag
  (`.github/workflows/container.yml`), with `evals/container_smoke.sh`, which
  runs the same checks under podman: it serves only with its token, refuses a
  foreign Host header, runs as the `ateam` user, and keeps teams across a
  restart. No key is passed in, so no agent runs.

### Fixed
- **The roster guard could keep an empty copy of a roster an agent rewrote
  during a turn.** It read the file while the agent was half way through
  writing it. The change was still put back; only the evidence was blank,
  about one time in fifteen. It now waits for the file to stop changing first.
- `CodexRunner` says when the `codex` it found is too old to have the
  app-server, and what to install.

## [4.4.0] - 2026-11-02

### Added
- **`CodexRunner` runs agents on OpenAI models** through Codex's `app-server`
  (`ateam.runner_codex:CodexRunner`; needs the Codex CLI 0.166 or later and
  `OPENAI_API_KEY`, no Python package). The key is read from the environment
  only; a ChatGPT sign-in on the machine is never used. Each turn starts the
  app-server, resumes the agent's thread and exits, so a supervisor restart
  loses nothing. Commands run in Codex's sandbox, which holds writes to the
  agent's workspaces; reads are not held. With no model named, the manager and
  anyone who reviews run on `gpt-6-astra` and everyone else on `gpt-6.1-sol`.
  Turns are recorded with tokens and no cost, and stop at 1,000,000 tokens
  unless `max_turn_tokens` says otherwise. `"provider"` in the runner config
  points it at another Responses API endpoint; only OpenAI's own has been
  tried. Tested against a local stand-in and on two live turns; a full
  supervised team on Codex has not yet run. See the README for the rest.
- First-run setup lists OpenAI as a runtime, saying what is missing.

### Fixed
- **Mid-turn notes were lost on Codex's code mode.** The team's MCP servers
  added notes (new mail, repeated calls) to a team tool's result, but on code
  mode the model only sees what its script prints, so a note was marked
  delivered and never read. A runner that delivers notes itself now sets
  `ATEAM_NOTES_BY_RUNNER=1` for its MCP servers, and `CodexRunner` delivers
  them through Codex's turn/steer.

## [4.3.0] - 2026-11-02

### Added
- **Recall checks whether a memory was used on real turns.** After a sample of
  turns that were offered memories (`memory_recall.used_sample`, 0.25 by
  default, 0 to turn it off), the judge compares each memory with what the turn
  did and logs `recall_used`: used, ignored, overruled on visible evidence, or
  not bearing. A turn with no record of its actions gets no verdict, which
  includes agy CLI turns with an empty final text. A correction that restates a
  memory the agent had been shown is logged as `recall_ignored`, at no extra
  model cost. The Memories page shows both.
- **Gateways.** A `gateway` on a roster entry or the Claude runner's config
  points that agent at an Anthropic-shaped endpoint, for other vendors' models.
  See the README for what it does not do; it has not yet run against a real
  gateway.
- `MixedRunner` takes `"runners": {spec: config}`, so each runner class can
  have its own settings.

### Changed
- **The memory block says a lesson can go out of date**, and to act on what the
  agent can see when the two disagree. In the bench, following a stale memory
  fell from 31% to 12%, and sound memories stayed at 92%.
- **New Claude defaults.** With no model named, the manager and anyone who
  reviews run on Opus 5.5 and everyone else on Sonnet 5.5, where everyone used
  to get Opus. A turn stops at $1 unless `max_budget_usd` says otherwise, where
  there used to be no cap. Set `model` and `max_budget_usd` to keep the old
  behaviour.
- Running out of AI Studio credit now stops the team. Google reports it as a
  429, which looked like a rate limit, so agents kept retrying and failing.
  OpenRouter's credit errors are recognised too.

### Fixed
- Cancelling a turn on a mixed team now reaches `ClaudeRunner`, which stops the
  turn where it runs. Before, the result was only discarded.

## [4.2.0] - 2026-11-02

### Added
- **Memories say where they came from.** Every save and merge an agent makes in
  a turn is logged with that turn, who its mail came from, and any outside
  content in the conversation so far: a web page, a download, or a tool a-team
  does not know. The Memories page shows it, as does the memory review, and
  recall adds a note to a marked memory: its ordinary advice stands, but a step
  that runs, sends, trusts or skips needs first-hand support. In the bench that
  note cost nothing on sound memories and took a planted bad step from 11% to
  0%. A mark survives a rewrite, a merge and a delete and
  restore; only the operator saving the memory clears it. This is a record, not
  a safety check: an unmarked memory can still have come from a file in the
  repository, and a shell command can reach the network in ways a-team does not
  see. Memories saved before this release have no stamp. New: `ateam/origin.py`,
  `<team_dir>/origin/`, and the `turn`, `from`, `mail` and `outside` fields in
  `memory_log.jsonl`.
- `bench/recall/behave.py` measures whether an agent acts on a memory it is
  given, what recall loses compared with always offering the right one, what
  the note costs on a sound memory, what it stops on a planted one, and whether
  an agent drops a stale memory when the request shows it no longer holds.
- `bench/recall/late_misses.py <team_dir>` replays a team's recall misses as if
  recall had also run during the turn, to show whether that is worth building.
  It writes nothing to the team, and also reads agy-team directories.

## [4.1.0] - 2026-11-02

### Added
- **Waiting on you can be cleared.** Supervisor notices had nobody to reply to,
  so they stayed on the list for good. Each message now has an ×, and notices
  have **Clear all**. A cleared message stays where it was sent, and an agent
  whose question is cleared is not told. `actions.clear_waiting` and
  `POST /api/clear_waiting` do this, for the operator only; `cleared.jsonl` in
  the team directory records it.
- A notice that an agent keeps failing clears itself once that agent's turn
  works. It carries the new message field `failing`.

### Fixed
- Notices no longer offer a thread reply, which had nowhere to go.

## [4.0.2] - 2026-11-01

### Changed
- **Claude turns cost about 60% less.** Agents are offered only the tools they
  may call. A one-word turn's context fell from 47,800 tokens to 19,300.
- On the board, **Waits for** takes task titles instead of ids, and a task's
  detail says which open tasks overlap the paths it expects to write.
- Someone with a link of their own no longer sees controls they cannot use, and
  the sidebar calls the operator's tasks that.

### Fixed
- Every page still said agychat, agyboard, agyadmin or agywiki in its corner.
- First run no longer shows a message box with nowhere to send to.
- Wrapped text in Waiting on you lines up, and the status line reads plainly.

## [4.0.1] - 2026-11-01

### Fixed
- **A Claude turn that reaches `max_budget_usd` or `max_turns` now stops.** It
  was treated as a failure and retried, so a cap meant to limit spending kept
  spending, with no cost recorded. The turn now ends, its cost is recorded, and
  the agent tells you with a `blocker`. Reply to have it continue.
- Update & restart works on a `uv tool` install, which has no pip.

## [4.0.0] - 2026-11-01

agy-team and agy-chat are now one product, a-team, in a new repository. It is
forked from agy-team 3.24.0 and agy-chat 0.19.0 with the history of both. Those
repositories stay where they are; nothing changes for a team that stays on them.

### Changed
- **One package and one command.** `pip install a-team` or pipx. `a-team` runs
  the app, and `a-team cli`, `a-team lifecycle`, `a-team starter`, `a-team
  people` and `a-team plugin` do the rest. The app's history is in
  `docs/app-changelog.md`.
- **Renamed:** the package to `ateam`, variables to `ATEAM_*`, the default home
  for teams to `~/a-teams`, a project's shared directory to `.a-team-shared`, and
  the plugin and its MCP servers to `a-team` and `ateam_*`.

### Added
- **It runs in a container.** The `Dockerfile` works with Docker or Podman, on
  an API key, as a non-root user. `a-team --start-teams` brings back every team
  that was not stopped on purpose, and SIGTERM now stops the app promptly.

### Kept working
- `AGYTEAM_*` and `AGYCHAT_*` variables are read when the new name is not set.
- `~/agy-teams` stays the default until `~/a-teams` exists, and an existing
  `.agy-team-shared` stays in use.
- A roster's `agyteam.*` runner, transport and memory specs, and
  `allowed_mcp_servers` entries, are read as their new names.

### Removed
- The `agyteam` and `agy-chat` commands, and importing `agyteam`. Code of your
  own that imports it needs `ateam` instead.

## [3.24.0] - 2026-11-01

### Changed
- Agents are told that several people may share a team: open with the asker's
  name, and name a conflict between people as a question instead of picking one.

## [3.23.0] - 2026-11-01

### Added
- **A team can start with no checkout.** `python -m agyteam.starter runtimes`
  says what this machine can run and what each runtime still needs, and
  `python -m agyteam.starter new <name>` creates a team on the starter roster.
  `actions.create_team` does the same for a client.
- **The plugin ships inside the package.** `python -m agyteam.install_plugin`
  installs it from a plain pip or pipx install.

### Changed
- The starter roster moved from a heredoc in `plugin/install.sh` to
  `plugin/starter_roster.json`.

## [3.22.0] - 2026-11-01

### Added
- **Tasks can wait for other tasks.** `blocked_by` names what must be done
  first. Until then only the operator can start the task, and its owner is
  told when it is ready, or when something it waits for fails.
- **Tasks can say where they will write.** `writes` names the paths a task
  expects to change, and `list_tasks` warns when open tasks overlap. It locks
  nothing.
- **`Record.needs_you()` lists what is waiting on a person:** a stopped team,
  unanswered questions and blockers, tasks the operator owns, and tasks an
  agent marked blocked.

### Changed
- Agents are now told how to write to the user: only when there is something
  to review, use, decide or unblock, result first, with `kind="question"` and
  `kind="blocker"` kept for what needs an answer.

## [3.21.0] - 2026-11-01

### Added
- **Mail reaches an agent during its turn.** Waking mail that arrives while an
  agent works is put in front of the model at its next step, with a pointer to
  `check_inbox`. It works on the agy CLI, Claude and the Antigravity SDK, and
  on any other host at the next team tool result. Quiet posts still wait.
- **Agents are told when they repeat themselves.** The same tool call 3, 5 or
  8 times in a row gets a note. It is advice and blocks nothing, except on the
  Antigravity SDK, where a repeat of 5 or more refuses one call to say so.
- The installed plugin now has a `hooks.json`, and `python -m agyteam.hook_step`
  is there for any host with step hooks. Both leave anything that is not a
  supervised turn alone.

### Fixed
- Mail read with `check_inbox` during a turn was lost if that turn then failed,
  unless the team used `inbox_pull`. It now goes back to the inbox.
- `check_inbox` during a turn no longer repeats the mail already in the prompt.

## [3.20.0] - 2026-11-01

### Added
- **Agents can run on Claude.** `agyteam.runner_claude:ClaudeRunner` wakes
  agents through the Claude Agent SDK, with an API key or Bedrock, Vertex or
  Foundry. A whole team can use it, or one agent in a mixed team. Agents keep
  the same team tools, brief, memory and roster policy. Each agent has a fixed
  tool list, and `tools_off` accepts either runtime's tool names. Sessions
  live in `<team_dir>/claude`, and provider keys are unset in agents' shells.
  Tool calls are audited, and each turn records its tokens, context size
  and cost. Install it with `pip install "agyteam[claude]"`.

### Changed
- A provider's `529` or "overloaded" now counts as a transient failure, and
  is waited out like a `429`.
- The cost report uses what a turn says it cost, when it says so.

## [3.19.0] - 2026-10-31

### Added
- **The record says what channel membership costs.** A quiet post wakes
  nobody, but it arrives with the agent's next wake. Each turn's `trigger`
  now records the quiet mail it carried: `quiet` with `msgs`, `bytes`,
  `deferred` and `channels`. `Record.quiet_mail(days=7)` adds it up by
  channel and agent, with a token estimate and its share of uncached input.
  `agyteam --cost` prints it. Turns before this release record nothing.

## [3.18.0] - 2026-10-29

### Added
- **Recall's judge can be a command of your own.** Set the roster's
  `memory_recall.judge_via` to an argv list, e.g.
  `["/usr/bin/python3", "/opt/team/judge.py"]`, for a team that has a model
  SDK but neither `GEMINI_API_KEY` nor the agy CLI. Before this, such a team
  had no judge, and recall offered only names. The prompt goes to the
  command's stdin, and its stdout is the JSON answer (fenced, or on the last
  line). A nonzero exit or no JSON falls back to names, as a failed judge
  does. It runs in an empty directory with the supervisor's environment.
  The same command writes memory cues and spots recall misses.

## [3.17.0] - 2026-10-29

### Added
- **Channels can wake some members on every post.** A channel's `wake` list
  names members that every post wakes, mention or not. Set it with
  `actions.set_channel_wake(team_dir, channel, agents)`. It suits an agent
  that owns a channel's work, such as a docs agent in `#docs`, so a teammate
  hands work off by posting there. Only members can be on it, and removing
  a member takes them off it. A post never wakes its author, and quiet kinds
  wake nobody. `list_channels` says who every post wakes ("every post wakes
  you" to that agent). Agent posts and thread replies wake the list too.
- `Transport.post` takes `wake`, the members to wake besides those
  mentioned. They are not recorded as mentions. A transport whose `post`
  lacks the parameter still works: they are passed as mentioned.

### Fixed
- A channel post of a quiet kind (`fyi`, `status`, `ack`) no longer
  reports the agents it mentions under `woke`: it wakes nobody.

## [3.16.0] - 2026-10-29

### Added
- **Agents can see who has room.** `list_teammates` now says how busy each
  teammate is, and the caller too. For each: working now or idle, waking
  mail not yet picked up, and the share of the last hour and day spent in
  turns. A last line says where the caller stands ("Over the last hour you
  were busier than 2 of your 3 teammates"). Its description asks agents to
  balance both ways. If you are among the busiest and part of your work
  fits a less busy teammate's role, hand that part over. If you are among
  the least busy, offer the busiest your help. The wake prompts point at
  it where they mention delegating.
- **`Record.load()`**: per agent, `working`, `since`, `waiting`, `busy`
  (shares of the last hour and day) and all-time `busy_s` and `turns`. It
  is timed from `turn` and `failure` events and the turn in flight. agy-chat
  shows it in its Agents list.

### Changed
- `doctor` counts `list_teammates` entries by their lines, so the caller's
  own line and the summary are not counted as teammates.

## [3.15.0] - 2026-10-29

### Changed
- **A running team's hop budget is a rate.** It allows `--max-hops` wakes
  in any `AGYTEAM_HOP_WINDOW` seconds (default 600). It used to reset only
  once no turn was in flight, which a busy team, or one long turn, may
  never reach. Until it did, no agent woke, and nothing said why. A
  one-shot episode still counts its whole run.
- **The user's messages are never held by the hop budget.** The budget
  bounds agents waking agents. A message from the user is a new stimulus,
  so it wakes its recipient even when the budget is spent.

### Added
- **`held` presence.** `Record.presence()` reports `held` for an agent
  whose waking mail the hop budget is holding back, with how much of the
  budget is used. The supervisor's heartbeat carries `held` and `hops`, and
  logs once when it starts holding mail.

## [3.14.0] - 2026-10-29

### Added
- **`Record.recall_stats(agent)`**: how recall has gone for an agent, from
  its `recall` and `recall_miss` events. Per day for the last two weeks:
  wakes, wakes offered memories, memories offered, misses, judge errors.
  Per memory, over all time: times offered, hinted and missed. Also the
  last ten misses, with the correction and what was learned. agy-chat
  draws it on its Memories page.

### Fixed
- **A reply to a reply disappeared.** An agent answering the operator's
  reply in a thread named that reply's id, as the tools invited. Stored
  as is, it hung off a message no client shows a thread under. It reached
  neither the channel nor the thread, and the agent believed it had
  answered. A reply now goes under its thread's first message, whichever
  message of the thread it names: channel posts, direct messages,
  agents' and the operator's alike. The record shows replies already
  stored under a reply in their thread too, so lost answers reappear. The
  `thread` parameter of `send_to_teammate` and `post_to_channel` now says
  that any message in the thread will do.

## [3.13.0] - 2026-10-29

### Added
- **Recall: an agent's own memories are put in front of it, per turn.**
  Agents saved good memories and did not read them back. Now, before a
  turn, the incoming mail is the query: candidates come from embeddings
  (with `GEMINI_API_KEY`) or from words and cues (without one). A Flash
  judge, through the API or the agy CLI, picks at most three that apply.
  They come with the wake as "You learned this N days ago (id): ...".
  Without a judge, only names are offered. Recall prepares off the dispatch
  loop and never holds up another agent. A correction that restates a
  memory the agent was not shown is a `recall_miss`, and teaches that
  memory the missed request's words. Off by default: `"memory_recall": true`
  in the roster. See "Recall" in the README and `bench/recall/`, which
  measured it on real memories with up to 10,000 distractors.
- `bench/recall/`: the recall benchmark. Its data stays outside the
  repository.

### Fixed
- **`./run` with a stale virtual environment.** After the system Python was
  upgraded (3.12 to 3.14), `.venv/bin/python` pointed at nothing, and `./run`
  failed. It now notices and rebuilds the environment.

## [3.12.0] - 2026-10-28

### Changed
- **Distills asked of a running supervisor run in parallel, alongside the
  team's work.** `--distill-all` or `--cycle-all` with a daemon running used
  to distill one agent after another, and the daemon's dispatch loop waited
  on each, so no other turn could start. Each requested distill or cycle now
  starts as a turn of its own when its agent is between turns, and is
  recorded when it ends. If the supervisor restarts mid-distill on a runtime
  that cannot resume turns, the request goes back on the queue, so the
  command waiting for it still gets an answer.

## [3.11.1] - 2026-10-27

### Fixed
- **An @mention in a direct message reached nobody.** A DM, or a reply in a
  thread under one, went only to the agent it was addressed to; `@ada can you
  help?` in it did not wake Ada, though the same words in a channel did.
  `actions.dm` now gives each agent it mentions (by name, alias, any case, or
  past a team prefix) a copy, which wakes it, saying it was mentioned in the
  operator's message to the other agent. Group words (`@all`, `@here`) do
  nothing in a DM.

## [3.11.0] - 2026-10-27

### Added
- **The task tree.** Tasks have a `parent` (one each, cycles refused), a
  `level` (set, or worked out from what is under it; names from roster
  `task_levels`, default task/project/program/portfolio), a `priority`
  (P0-P4), `auto_close`, and `archived` (the operator's; it hides a whole
  branch). `tasks.tree()` and `Record.task_tree()` give each task's path,
  closed/total under it, and tokens over its subtree.
- When a parent's last open child closes, its owner (or the operator) is
  told once, or, with `auto_close`, the parent closes itself (not over a
  failed child) and the check climbs.
- `create_task(parent, level, priority, auto_close)`. With no parent, a new
  task goes under the one task the agent is working on, if there is exactly
  one, and the result says so. `update_task` takes the same fields;
  `list_tasks(under=<id>)` shows a branch as a tree and hides archived
  branches unless asked.
- A wake about a task includes what it is part of, and the top task's note.
- The brief has a short section on how work is organised.
- `actions.migrate_project_labels`: each project label becomes a level-1
  parent task, its tasks under it; all-closed labels become closed, archived
  parents. `actions.create_task` and `update_task` take the tree fields.

### Changed
- `project` is optional on `create_task` (a subtask takes its parent's).
  Existing callers that pass it are unaffected.

## [3.10.1] - 2026-10-27

### Fixed
- **`Record.task_usage` counted no inferred turns on real teams.** Tasks are
  stamped in UTC ISO and turns in local time, and the two were compared as
  text. The 3.9.0 memory-review evidence compared failed tasks the same
  way. Both now compare instants (`agyteam.record.instant`, which reads
  either kind).
- **An alias set through `actions.update_agent` was never used.** It wrote an
  `aliases` list, but the roster's field is `alias`, a single name the agent
  is shown by and answers to as @alias. `AGENT_FIELDS` now has `alias`.

## [3.10.0] - 2026-10-27

### Added
- **Tokens per task**: `Record.task_usage()`. A turn woken by a message
  naming tasks counts toward them ("direct"). Any other turn counts toward
  the task its agent owned and was working (claimed, running, review) when
  the turn ended ("inferred"). Each task also gets a per-agent breakdown.
- **Bookmarks**: on channels (`actions.set_channel_bookmarks`, kept in
  `channels.json`) and on tasks (`links`, `actions.set_task_links`), each a
  list of `{title, url}`.
- **Pictures** for agents and the operator: `actions.set_avatar`,
  `Record.avatars()`, files in `<team_dir>/avatars/<name>.<ext>`.
- **Attachments**: `actions.attach` keeps a file the operator shares in
  `<team_dir>/attachments/` and returns its absolute path for the message.

## [3.9.0] - 2026-10-23

### Added
- **`memory_model` / `memory_effort`** (roster, or one agent's entry): run
  distill, memory review and retro turns on a stronger model while work
  turns stay on the agent's own. It needs a runtime that switches models
  inside a conversation (the agy CLI does); otherwise memory turns run on the
  agent's model and the supervisor says so once. A model the runner does not
  offer is skipped. Runners gain `set_turn_model` / `clear_turn_model`,
  honoured by `resolved_model`. `actions.update_agent` and `add_agent` take
  `memory_model` and `memory_effort`.
- **Memory reviews see outcomes**: reviews that asked the agent for changes,
  its failed or cancelled tasks, and what the operator told it since its
  last review. Failed turns are left out, as runtime noise.

### Changed
- Distill and review prompts, the brief, and `save_memory` / `merge_memories`
  descriptions ask for lessons that carry over to the next task rather than
  values true only for this one, and for descriptions that say when a memory
  applies. A review treats a never-read memory as possibly unfindable and
  fixes its description first. From a memory-writing benchmark (Letta's
  FrontierMemory): memories that recorded one task's file sizes did not
  transfer, and one lesson split four ways was never found.

## [3.8.0] - 2026-10-22

### Added
- **Memory history**: the memory server logs every save, read, delete and
  merge to `memory_log.jsonl`, whatever the store. `Record.memory_stats(agent)`
  gives age, revisions, reads and last read; for history from before the
  log, it counts the event log's `read_memory` calls.
- **`merge_memories`** (a memory tool): replaces several memories with one
  the agent writes, archives the sources, and keeps their history.
- **Memory archive**: `delete_memory` and merges copy what they remove to
  `memory_archive/<agent>/`. `actions.archived_memories` and
  `actions.restore_memory` list and restore them.
- **Memory review turns**: an agent goes over its own memories with their
  history and `review_memory`'s flags, and merges, rewrites and deletes. The
  daemon queues one at an agent's first 10 memories, at 25/50/100/200/400/800,
  and weekly after changes, and runs it between the agent's turns.
  `--review-memory`, `--review-memory-all` and `actions.review_memory` ask
  for one. Each is recorded as a `memory_review` event. Configure with
  roster `memory_review` (or `false`).
- **Norm candidates**: `Record.norm_candidates()` finds the same lesson in
  three or more agents' memories, and retros are shown them.
- `Record.memory_archive(agent)`; `memory_log.open_store(team_dir, agent)`.

### Fixed
- The operator's memory actions opened an agent's store by the default
  layout. Where `AGYTEAM_DURABLE_DIR` puts agents elsewhere, they edited
  memories the agent never reads. They now open the store the agent's own
  server does.

## [3.7.1] - 2026-10-20

### Fixed
- The implicit `#general` (a team with no `channels.json`) was written down
  as a fixed member list the first time any channel changed, so agents added
  after that were not in it. It is now saved as an `everyone` channel and
  keeps following the roster.

## [3.7.0] - 2026-10-20

### Added
- **Graceful restart.** A restart request (`./run restart`,
  `python -m agyteam.supervisor --restart`, `actions.restart`, or SIGHUP)
  makes the driving supervisor drain: it starts no new turns, mail keeps
  queuing, and the turns in flight finish. Then a daemon restarts itself in
  place (same pid, arguments and environment) on the installed code.
  `--now` cancels the turns in flight instead, and their mail goes back. The
  heartbeat now records `version`, `started`, `restartable` and `draining`.
- **`python -m agyteam.update` / `./run update`**: one command that updates
  agy-team where it is installed (a git checkout fast-forwards, a pip
  install from git moves to the newest release tag), reinstalls the plugin,
  and gracefully restarts every running team. For a supervisor from before
  graceful restart, it waits for a moment with no turn in flight, then stops
  and starts it. `--check`, `--to`, `--now`, `--no-code`, `--no-restart`,
  `--no-wait`, `--team-dir`, `--json`.
- **Channels that follow the roster**: `everyone=True` on `create_channel`
  and `set_members`. An agent added later is a member without anyone adding
  it.
- **Admin actions** for clients: `add_agent` (which joins every open channel
  by default), `update_agent` (role, model, effort, principal, tools_off,
  aliases), `remove_agent` (which also removes it from channels), `norms` /
  `set_norms` (recorded as a `norms_edited` event), and `memories` /
  `save_memory` / `delete_memory` for one agent's store, opened for that
  team. Roster edits go through the roster's history, so a running
  supervisor adopts them instead of putting them back.

### Changed
- README: `./run stop` cancels the turns in flight; to keep them, use
  restart or update.

## [3.6.0] - 2026-10-20

### Added
- **Distills asked from outside go through the driving supervisor.**
  `--distill`, `--cycle`, `--distill-all`, `--cycle-all` and
  `--nightly-distill` (and `agyteam.supervisor.distill`/`cycle`) queue the
  request (`distill_requests.jsonl`) when another supervisor drives the
  team. That supervisor runs it when the agent is between turns and records
  `distill_request_done`. The command waits for the result, up to
  `AGYTEAM_DISTILL_WAIT_S` (900 s); anything not done by then still runs.
- **Distill ahead of an allowance running out.** At the first warning that a
  model is near its weekly `model_allowance`, the agents working on it
  distill and keep their conversations, while the model can still run a
  distill. Turn it off with roster `"policy": {"distill_before_allowance": false}`.
- `boolean` arguments in the tool servers, with `mcp_base.flag` and
  `mcp_base.check_args`.

### Changed
- `create_task`'s `requires_review` is declared a boolean. The words for one
  (`"true"`, `"no"`, ...) are still accepted.
- Tool arguments are checked against their declared types before a handler
  sees them. A number or boolean sent for a text field becomes its text,
  rather than being dropped as if it were absent.

### Fixed
- **`--distill` ran into a live turn.** While a daemon drove the team, it
  woke the agent in a second process alongside the daemon's turn, because it
  checked only its own process's turns. `distill` also never checked for a
  turn in flight at all; now it skips the agent.
- **`requires_review: true` made an ungated task.** A JSON boolean, or a typo
  like `"ttur"`, was read as false without a word. Now `true` is true, and
  anything that is not a yes or a no is an error the agent sees.
- A distill that fails because the account is out of quota says so: the
  conversation is kept, so nothing is lost, and a `distill_deferred` event
  records it.
- README: training is described as it works. Most of it is conversation that
  lands in each agent's memories; a retro writes only norms and is optional.
  The distillation triggers are listed in full. Model examples no longer use
  names the CLI refuses.
- The brief no longer tells agents their memory index is "below"; they read
  it with `memory_index`.

## [3.5.0] - 2026-10-20

### Added
- **Channels can be archived, deleted and restored**
  (`actions.archive_channel` / `delete_channel` / `restore_channel`,
  `channels.archive` / `delete` / `restore`).
  - Archived: read-only, and out of every agent's `list_channels` and
    channel membership.
  - Deleted: archived, and hidden from clients (`Record.search` skips it).
    Its messages stay in `bus.jsonl`, the team's record, and the name cannot
    be reused until it is restored.
  - `channels.active()` lists the open ones.
- **Tasks name the channel they are discussed in** (`channel=` on
  `create_task` / `update_task`, the tools included; `""` unlinks). A status
  change on a linked task is noted in that channel as a quiet `status`
  line.
- **Board statuses.** `icebox` is parked work: not claimable, reminders
  paused, and only the operator takes it back out. `review` means done and
  waiting on an approved review; `complete_task` still requires the review.
  Common spellings map to them (`backlog`, `in_review`, ...). `rank` orders a
  task within a column.
- **The operator moves any card**: `actions.create_task` and
  `actions.update_task` (status, owner, title, project, note, channel,
  deadline, rank); `tasks.update(by="user")` is no longer limited to unowned
  tasks. The owner is told, waking them when they should stop (iced or
  cancelled mid-work) or start (handed the task).
- **Aliases.** An agent's roster `alias` resolves wherever a name is taken
  (`send_to_teammate`, @mentions), is shown in the brief and in
  `list_teammates`, and is for clients to display. `roster.aliases()`, and
  `resolve_name(..., aliases)`.
- **The operator's handle.** The roster's `operator: {"name", "handles"}`
  tells agents who the user is. `send_to_teammate(to="Matt")` reaches the
  user, and `@Matt` in a channel is recorded in the post's `mentions` as
  `user` without waking anyone. The user stays `user` on the bus.
  `Record.operator()`.

### Changed
- The roster refuses a name, alias or operator handle that would mean two
  things (ignoring case), and an alias that is not a handle.
- @mentions may contain a dot inside the name (`@a.b`), not at its end.

## [3.4.0] - 2026-10-20

### Added
- `runner.team_runner(team_dir, runner_flag=None)` -> `(spec, config,
  source)`: the one resolver for which runner a team runs on (`--runner`,
  then the roster's `supervisor` block, then `AGYTEAM_RUNNER`, then the
  default). `list_models`, `model_available`, `runner_class` and
  `capabilities` take `team_dir=` to answer for that team.
- `Supervisor(..., inspect_only=True)`: a supervisor that reports and never
  drives the team or touches its ledger.
- `--status` also shows whose turn is running, read from `inflight.json`
  without loading it.

### Fixed
- **`--status` wiped `inflight.json`.** It builds a Supervisor to peek at
  the queues, and `Supervisor.__init__` loaded the ledger with a runner that
  resumes nothing, before anything asked whether this process drives the
  team. Every live turn was recorded as a failure ("supervisor restarted
  mid-turn") and an empty ledger was saved over the live supervisor's.
  `--distill`, a retro, and a supervisor about to be refused the team did
  the same. A Supervisor now loads and saves the ledger only when no other
  process holds the team's lock, or once it claims the team itself.
- **Which models the team's runner offers was asked of the environment.**
  `Record.models()`, `agyteam.doctor`, `agyteam.models` (`available`,
  `list`, and the check on `set`), `lifecycle status` and `my_capabilities`
  never read the roster's `supervisor` block. From any process without
  `AGYTEAM_RUNNER` / `AGYTEAM_RUNNER_CONFIG` exported, such as a chat
  client, they described the default runner or nothing, and a model change
  was checked against the wrong runner. They now use `team_runner`, and
  doctor prints where the runner came from.

## [3.3.0] - 2026-10-19

### Fixed
- **Under `--inbox-pull`, mail pulled mid-turn is no longer lost when the
  turn fails.** Reported downstream: an agent sends an early progress note,
  a teammate's mail arrives while the turn runs, and the agent's
  `check_inbox` marks it read; if the turn then crashed, that mail was
  gone. The supervisor restored only the batch it had woken the agent with,
  and only if the turn had sent nothing. Now `check_inbox` writes a receipt
  before marking anything read during a supervised turn. A failed turn gives
  back everything on its receipt that is no longer in the inbox, once, and
  a successful turn discards the receipt.

### Added
- `Message.redelivered` (`"redelivered"` in the inbox record): set on mail a
  failed turn took, saying what that turn had already sent, and shown first
  when the agent reads it, so a turn that answered before it died is not
  simply redone.
- `receipts/<agent>.jsonl` in the team directory: one exists only while a
  supervised pull-mode turn is open, so an agent run by hand keeps none.

## [3.2.0] - 2026-10-19

### Added
- **The supervisor guards the roster.** Every permission an agent has is in
  `roster.json`, and an agent with a shell runs as the user who wrote it.
  Reported from downstream testing: a test agent given no permissions was
  refused an edit, built the roster's path in pieces to get past the check,
  granted itself access, edited the test script and finished its task. The
  supervisor now keeps the roster it accepted. A change made while an
  agent's turn is running, and not written by agyteam itself, is put back
  and kept as `roster.rejected-<time>.json`. A `roster_reverted` event names
  the agents that were running, and the operator gets a `blocker` message,
  which does not count as an answer to the user. A change made while no turn
  runs is adopted. This is detection, not a boundary: see Known limits in
  the README.
- `roster_history.jsonl`: `roster.save` records each write by content hash
  before making it. This is how the supervisor tells `lifecycle` and the
  roster admin tools apart from an agent editing the file.
- `agyteam.doctor` warns when `roster.json` is writable by the user it runs
  as, which on most setups means by the agents too.

### Fixed
- **No agent writes the team directory, confined or not.** The rule was
  enforced only for agents with a declared workspace, so an agent given no
  permissions was the one agent that could rewrite them. File tools and
  shell writes (redirects, `cp`/`mv` destinations, `sed -i`, ...) into it are
  refused, on the SDK runner too. The refusal asks the agent to request a
  missing permission rather than work around it.
- **`agyteam.doctor` no longer eats a live agent's mail.** It probed
  `check_inbox` as the first agent on the roster, which marked that agent's
  real messages read and printed them only to the preflight. It now sees and
  acknowledges only its own probe, and sends the probe as `fyi` so a running
  supervisor does not wake the agent for it.
- `roster.save` writes atomically. A reader between the truncate and the
  write saw an empty roster.

## [3.1.0] - 2026-10-18

### Added
- **The roster's `supervisor` block** (`runner`, `runner_config`, `manager`,
  `inbox_pull`): how a team is run, kept with the team, so a supervisor
  started by a client with none of your environment runs it the same way.
  `--runner` on the supervisor command line; the heartbeat records the
  runner, the manager and where each came from.
- **Team-prefixed handles**: `@Ada` and `send_to_teammate(to="Ada")` reach
  `at-Ada` when exactly one agent can be meant (exact name, then case, then a
  name ending in `-Ada`, `_Ada` or `.Ada`). `agyteam.roster.resolve_name`.
- **Document-fit reviews**: `record_review(..., deliverable, criteria)` --
  each requirement either quoted verbatim from the deliverable (checked) or
  marked unmet with a reason. Approval needs every criterion met; a request
  for changes needs at least one unmet. Code reviews still need a proof.
- **`switches_model_in_conversation`** on runners: a model change on a
  runtime that switches in place (the agy CLI, measured) keeps the
  conversation instead of distilling and cycling it. Host runners declare it
  in `capabilities`.
- **One supervisor per team**: a lock (`supervisor.lock`); a second
  supervisor refuses the team and leaves the live one's heartbeat alone.

### Fixed
- A supervisor started by a client used the client's runner and a guessed
  manager: wakes failed and conversations were lost.
- Turns that time out no longer count toward retiring a conversation; a slow
  agent kept its context.
- Under `inbox_pull`, a successful turn no longer marks the whole batch read;
  only the pages the agent opened are.
- **Review gate window**: an author's work now counts from the start of the
  episode in progress, or of the one that just finished when no new one has
  begun. It used to count only from the end of the last finished episode,
  so a review recorded after an episode could not name work done in it, and
  passed only when the work and the episode's end fell in the same second.
  Work from before that episode still does not count. The supervisor records
  an `episode_start` event in `events.jsonl` to mark where the window opens.
  For episodes recorded before this change, the start is worked out from
  their `duration_s`.

## [3.0.0] - 2026-10-18

The first release under semantic versioning. `v1.0` and `v2.0` were
milestone tags on the early (claw-agy) code and are not semver releases;
anything installed between them and this tag reported itself as `0.1.0`.
Everything below is relative to `v2.0`.

### Added
- **Teams of persistent agents on Antigravity**: the agy CLI plugin (four MCP
  servers: bus, memory, introspection, tasks), a reactive supervisor that
  wakes agents when mail arrives, and runners for the agy CLI (default), the
  Antigravity SDK, a mixed roster, and any host that can start, deliver and
  signal a turn (`HostRunner`, with generic `hook_pre_tool_use` and
  `hook_stop`).
- **Concurrent dispatch** with backoff, escalation, a persisted in-flight
  ledger that survives restarts, and a budget halt that stops the team once
  when the account is exhausted.
- **Channels, threads and mention-only waking** (`post_to_channel`,
  `list_channels`, `thread=`): unmentioned members read posts on their next
  wake; a byte budget keeps a busy channel out of the wake prompt.
- **Tasks and reminders** (`check_after`, `until` conditions, deadlines,
  evidence-gated closure, `requires_review`, operator waivers) and a sweep any
  scheduler can run.
- **Review gate**: proof-backed reviews by a teammate who is not the author
  and who did no work outside the record.
- **Learning**: memory with provenance and correction, retrospectives that
  write team norms, distill-then-cycle (auto, all-at-once in parallel, and a
  scheduled daily sweep), and `distill_only` for hosts that compact.
- **Model choice**: `python -m agyteam.models` overrides per agent or for
  everyone, with expiry, applied as a cycle; weekly usage and allowances;
  each runner's model list, with unavailable models refused and flagged.
- **One policy for every runtime** (`tools_off`, `allowed_send_to`,
  `assigns_tasks`, workspaces, `refuse_paths`, private agent workspaces,
  detach/forbidden commands, MCP-server scoping, inbound message caps).
- **A client API**: `agyteam.record` (read and follow the record) and
  `agyteam.actions` (operator actions); [agy-chat](https://github.com/roelle/agy-chat)
  is built on it.
- **Instruments**: an offline suite of ~600 tests, live evals, a cold-start
  convergence bench with an execution-graded scale suite run in Docker, and
  `agyteam.doctor`.

### Changed
- The Antigravity SDK is an optional extra: `pip install "agyteam[sdk]"`. The
  core is standard library only.
- The seeded roster pins no model; choose them with `agyteam.models`.
- No runner declares `supports_containment`: an agent with a shell is
  contained only by what contains the process.
- Licensed under MIT.

## [2.0] - 2026-09-08
Milestone tag (pre-semver): session cycling, role-scoped tools, an optional
MCP memory server.

## [1.0] - 2026-09-08
Milestone tag (pre-semver).

[Unreleased]: https://github.com/roelle/a-team/compare/v4.18.0...HEAD
[4.18.0]: https://github.com/roelle/a-team/compare/v4.17.0...v4.18.0
[4.17.0]: https://github.com/roelle/a-team/compare/v4.16.0...v4.17.0
[4.16.0]: https://github.com/roelle/a-team/compare/v4.15.0...v4.16.0
[4.15.0]: https://github.com/roelle/a-team/compare/v4.14.0...v4.15.0
[4.14.0]: https://github.com/roelle/a-team/compare/v4.13.0...v4.14.0
[4.13.0]: https://github.com/roelle/a-team/compare/v4.12.0...v4.13.0
[4.12.0]: https://github.com/roelle/a-team/compare/v4.11.0...v4.12.0
[4.11.0]: https://github.com/roelle/a-team/compare/v4.10.0...v4.11.0
[4.10.0]: https://github.com/roelle/a-team/compare/v4.9.0...v4.10.0
[4.9.0]: https://github.com/roelle/a-team/compare/v4.8.1...v4.9.0
[4.8.1]: https://github.com/roelle/a-team/compare/v4.8.0...v4.8.1
[4.8.0]: https://github.com/roelle/a-team/compare/v4.7.0...v4.8.0
[4.7.0]: https://github.com/roelle/a-team/compare/v4.6.0...v4.7.0
[4.6.0]: https://github.com/roelle/a-team/compare/v4.5.0...v4.6.0
[4.5.0]: https://github.com/roelle/a-team/compare/v4.4.0...v4.5.0
[4.4.0]: https://github.com/roelle/a-team/compare/v4.3.0...v4.4.0
[4.3.0]: https://github.com/roelle/a-team/compare/v4.2.0...v4.3.0
[4.2.0]: https://github.com/roelle/a-team/compare/v4.1.0...v4.2.0
[4.1.0]: https://github.com/roelle/a-team/compare/v4.0.2...v4.1.0
[4.0.2]: https://github.com/roelle/a-team/compare/v4.0.1...v4.0.2
[4.0.1]: https://github.com/roelle/a-team/compare/v4.0.0...v4.0.1
[4.0.0]: https://github.com/roelle/a-team/releases/tag/v4.0.0
[3.24.0]: https://github.com/roelle/agy-team/compare/v3.23.0...v3.24.0
[3.23.0]: https://github.com/roelle/agy-team/compare/v3.22.0...v3.23.0
[3.22.0]: https://github.com/roelle/agy-team/compare/v3.21.0...v3.22.0
[3.21.0]: https://github.com/roelle/agy-team/compare/v3.20.0...v3.21.0
[3.20.0]: https://github.com/roelle/agy-team/compare/v3.19.0...v3.20.0
[3.19.0]: https://github.com/roelle/agy-team/compare/v3.18.0...v3.19.0
[3.18.0]: https://github.com/roelle/agy-team/compare/v3.17.0...v3.18.0
[3.17.0]: https://github.com/roelle/agy-team/compare/v3.16.0...v3.17.0
[3.16.0]: https://github.com/roelle/agy-team/compare/v3.15.0...v3.16.0
[3.15.0]: https://github.com/roelle/agy-team/compare/v3.14.0...v3.15.0
[3.14.0]: https://github.com/roelle/agy-team/compare/v3.13.0...v3.14.0
[3.13.0]: https://github.com/roelle/agy-team/compare/v3.12.0...v3.13.0
[3.12.0]: https://github.com/roelle/agy-team/compare/v3.11.1...v3.12.0
[3.11.1]: https://github.com/roelle/agy-team/compare/v3.11.0...v3.11.1
[3.11.0]: https://github.com/roelle/agy-team/compare/v3.10.1...v3.11.0
[3.10.1]: https://github.com/roelle/agy-team/compare/v3.10.0...v3.10.1
[3.10.0]: https://github.com/roelle/agy-team/compare/v3.9.0...v3.10.0
[3.9.0]: https://github.com/roelle/agy-team/compare/v3.8.0...v3.9.0
[3.8.0]: https://github.com/roelle/agy-team/compare/v3.7.1...v3.8.0
[3.7.1]: https://github.com/roelle/agy-team/compare/v3.7.0...v3.7.1
[3.7.0]: https://github.com/roelle/agy-team/compare/v3.6.0...v3.7.0
[3.6.0]: https://github.com/roelle/agy-team/compare/v3.5.0...v3.6.0
[3.5.0]: https://github.com/roelle/agy-team/compare/v3.4.0...v3.5.0
[3.4.0]: https://github.com/roelle/agy-team/compare/v3.3.0...v3.4.0
[3.3.0]: https://github.com/roelle/agy-team/compare/v3.2.0...v3.3.0
[3.2.0]: https://github.com/roelle/agy-team/compare/v3.1.0...v3.2.0
[3.1.0]: https://github.com/roelle/agy-team/compare/v3.0.0...v3.1.0
[3.0.0]: https://github.com/roelle/agy-team/compare/v2.0...v3.0.0
[2.0]: https://github.com/roelle/agy-team/compare/v1.0...v2.0
[1.0]: https://github.com/roelle/agy-team/releases/tag/v1.0
