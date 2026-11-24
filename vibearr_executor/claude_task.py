"""Image-owned Claude invocation; source permissions remain the worker's boundary."""

import json
import os


READ_ONLY_ROLES = {
    "intake",
    "specification",
    "reviewer-code",
    "reviewer-security",
    "reviewer-ux",
    "reviewer-ui",
    "decision",
}
ALTERNATE_AUTH = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)


def command(model, home, *, factory_role=None):
    role = os.environ.get("VIBEARR_FACTORY_ROLE")
    if factory_role is not None and factory_role != role:
        raise ValueError("Claude role differs from the registered worker profile.")
    if role is not None and role not in READ_ONLY_ROLES | {"build"}:
        raise ValueError("Unsupported registered Claude factory role.")
    if any(os.environ.get(name) for name in ALTERNATE_AUTH):
        raise ValueError(
            "Claude requires only the selected managed subscription login."
        )
    os.environ.update(
        CLAUDE_CONFIG_DIR=str(home / ".claude"),
        DISABLE_AUTOUPDATER="1",
        DISABLE_TELEMETRY="1",
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
    )
    read_only = role in READ_ONLY_ROLES
    arguments = [
        "/usr/local/bin/claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        model,
        "--permission-mode",
        "dontAsk" if read_only else "bypassPermissions",
        "--setting-sources",
        "user",
        "--settings",
        json.dumps({"disableAllHooks": True, "enabledPlugins": {}}),
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--disable-slash-commands",
        "--no-chrome",
        "--no-session-persistence",
    ]
    if read_only:
        arguments.extend(
            ["--tools", "Read,Glob,Grep", "--allowedTools", "Read,Glob,Grep"]
        )
    return arguments
