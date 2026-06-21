// opencode plugin: PreToolUse enforcement adapter for mcm-engine.
//
// What this does:
//   - Fires on every tool.execute.before in opencode.
//   - Shells out to `mcm-engine hook`, which holds the canonical
//     enforcement logic (warn at 8 built-in calls, block at 20 — Bash
//     exempt). The Python script is the single source of truth; this
//     file is a thin translation layer between opencode's plugin API
//     and the Python CLI's stdin/exit-code contract.
//   - If `mcm-engine hook` exits non-zero, throws an Error — opencode
//     treats the throw as a block on the tool call. stderr from the
//     Python script becomes the error message.
//
// Install:
//   Copy this file to ONE of:
//     .opencode/plugins/mcp-enforcement.js              (this project only)
//     ~/.config/opencode/plugins/mcp-enforcement.js     (all opencode projects)
//   No other config needed. The `mcm-engine` binary must be on PATH —
//   `uv tool install mcm-engine` puts it there.
//
// Notes for opencode users:
//   - opencode names its built-ins lowercase (edit / write / bash /
//     apply_patch). The Python hook recognizes these natively.
//   - opencode names MCP tools `<server>_<tool>` (e.g. `mcm-engine_search`).
//     The Python hook recognizes that format too. No translation needed.
//   - For warnings (allow but message the user), stderr is written to
//     console.warn so it surfaces in the opencode log without blocking.

export const MCMEnforcement = async ({ directory }) => {
  return {
    "tool.execute.before": async (input, _output) => {
      const event = JSON.stringify({
        tool_name: input.tool,
        session_id: input.sessionID,
        cwd: directory,
      });

      const proc = Bun.spawn(["mcm-engine", "hook"], {
        stdin: "pipe",
        stdout: "ignore",
        stderr: "pipe",
      });
      proc.stdin.write(event);
      proc.stdin.end();

      const exitCode = await proc.exited;
      const stderr = await new Response(proc.stderr).text();

      if (exitCode !== 0) {
        // Non-zero exit = block. Message comes from mcm-engine's stderr.
        throw new Error(
          stderr.trim() || `mcm-engine hook blocked ${input.tool}`,
        );
      }

      // Zero exit with stderr = non-blocking warning. Surface it.
      if (stderr.trim().length > 0) {
        console.warn(stderr.trim());
      }
    },
  };
};
