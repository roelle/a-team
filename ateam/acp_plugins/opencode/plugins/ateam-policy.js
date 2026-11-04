// The team's policy on every OpenCode tool call, before it runs.
//
// a-team starts `opencode acp` with OPENCODE_CONFIG_DIR pointing at the
// directory above this one, so OpenCode loads this plugin beside whatever the
// user's own config has. ACP alone lets a client refuse only the calls an
// agent asks permission for, and a file read's request carries no path; here
// every call is seen with its full arguments, and a refusal reaches the model
// as the call's error, with the reason. The check itself is a-team's
// (ateam.runner_acp check -> ateam.policy), so it is the same rule every
// other runtime applies.
import { spawnSync } from "node:child_process"

const KEYS = (process.env.ATEAM_STRIP_KEYS || "").split(",").filter(Boolean)

export const AteamPolicy = async () => ({
  "tool.execute.before": async (input, output) => {
    const agent = process.env.ATEAM_AGENT
    const python = process.env.ATEAM_PYTHON_BIN
    if (!agent || !python) return
    const r = spawnSync(python, ["-m", "ateam.runner_acp", "check"], {
      input: JSON.stringify({ agent, tool: input.tool, args: output.args || {}, cwd: process.cwd() }),
      env: process.env, encoding: "utf8", timeout: 30000,
    })
    if (r.status === 2) throw new Error((r.stdout || "").trim() || "[refused by the team's policy]")
  },
  // Keys that run agents stay out of the commands agents run.
  "shell.env": async (input, output) => {
    if (!output || !output.env) return
    for (const k of KEYS) output.env[k] = ""
  },
})
