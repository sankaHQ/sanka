# Sanka Migrate MCP

Credential-free MCP tools for Sanka Migrate's public software lifecycle,
cost, and comparison research, plus the free migration assessment handoff.

> **Alpha:** version `0.1.0a2` is published on
> [PyPI](https://pypi.org/project/sanka-migrate-mcp/). MCP tool contracts may
> still change before `1.0`.

## Configure

Run the standalone server with `uvx`:

```json
{
  "mcpServers": {
    "sanka-migrate": {
      "command": "uvx",
      "args": ["sanka-migrate-mcp"]
    }
  }
}
```

No API key is required. Set `SANKA_MIGRATE_API_BASE` only when testing an
alternate Sanka Migrate API deployment; the default is
`https://api.sanka.com/v2/migrate`.

## Tools

| Tool | Behavior |
|---|---|
| `sanka_migrate_research_eol` | Read product shutdown, support, maintenance, and lifecycle events. |
| `sanka_migrate_research_tco` | Read cited software pricing and cost benchmarks. |
| `sanka_migrate_research_compare` | Compare cited export, import, and operational facts for up to ten selected platforms. |
| `sanka_migrate_assess` | **Creates** one free migration assessment and returns the signup handoff URL. |

The three research tools are read-only. They are keyless and rate-limited per
IP. Lists are bounded to 50 items and report `truncated: true` when a narrower
query is needed. Every served claim keeps its vendor source URL and verification
date; cite those sources when using the data. A rate-limit response is returned
as structured data with the retry delay so agents can back off safely.

`sanka_migrate_assess` is the only write. It records the answers supplied to the
tool and returns an assessment identifier, signup URL, and next step; it does
not create an account or start a migration.

## License boundary

This package is Apache-2.0 and is deliberately outside the `sanka.*` runtime
namespace. It is a small HTTP client over the public API and never imports the
AGPL migration runtime. Its direct runtime dependencies are the MCP Python SDK,
httpx, and Pydantic for bounded tool schemas; Pydantic Settings is temporarily
bounded below 2.15 to avoid its upstream FastMCP lifespan warning on startup.
Repository checks enforce that boundary.
