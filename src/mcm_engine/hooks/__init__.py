"""Agent-harness integration hooks (Claude Code PreToolUse, etc.).

The MCP server's built-in nudge system (``mcm_engine.tracker``) can only
see MCP tool calls. It's blind to built-in agent tools — Edit, Write,
NotebookEdit, Bash — so the model can flood an MCP-first project with
edits without ever calling ``search`` or ``report_error``. The hook in
this package closes that gap from the agent harness side: it runs on
every Edit/Write/Bash and tracks budget against compliance MCP reads.
"""
