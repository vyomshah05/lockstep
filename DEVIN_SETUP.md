# Lockstep MCP Server - Devin CLI Setup

This guide provides the technical configuration details for the Lockstep MCP server with Devin CLI.

## Configuration Overview

The MCP server configuration is defined in `.devin/config.json` and uses environment variables from your `.env` file.

## Configuration File Structure

The `.devin/config.json` file contains:

```json
{
  "mcpServers": {
    "lockstep": {
      "command": "/opt/anaconda3/envs/cs178/bin/python",
      "args": ["-m", "server"],
      "cwd": "/Users/doubledogok/calaihack2026/cal-aihacks-2026/cal-ai-2026",
      "env": {
        "REDIS_HOST": "${env:REDIS_HOST}",
        "REDIS_PORT": "${env:REDIS_PORT}",
        "REDIS_USERNAME": "${env:REDIS_USERNAME}",
        "REDIS_PASSWORD": "${env:REDIS_PASSWORD}",
        "SUPABASE_URL": "${env:SUPABASE_URL}",
        "SUPABASE_SERVICE_KEY": "${env:SUPABASE_SERVICE_KEY}",
        "ANTHROPIC_API_KEY": "${env:ANTHROPIC_API_KEY}",
        "RERANK_MODEL": "${env:RERANK_MODEL}",
        "EMBED_MODEL": "${env:EMBED_MODEL}",
        "EMBED_DIM": "${env:EMBED_DIM}",
        "CACHE_CAPACITY": "${env:CACHE_CAPACITY}",
        "CACHE_THETA": "${env:CACHE_THETA}",
        "DOCS_TTL_SECONDS": "${env:DOCS_TTL_SECONDS}",
        "RECO_TTL_SECONDS": "${env:RECO_TTL_SECONDS}",
        "DOCS_TOP_K": "${env:DOCS_TOP_K}",
        "LIBS_TOP_K": "${env:LIBS_TOP_K}",
        "LOCKSTEP_HOST": "${env:LOCKSTEP_HOST}",
        "LOCKSTEP_PORT": "${env:LOCKSTEP_PORT}"
      }
    }
  }
}
```

## Key Configuration Details

### Working Directory (`cwd`)
The `cwd` field ensures the server starts in the correct directory to access:
- The `.env` file
- Python modules (`config.py`, `server.py`, etc.)
- The `tools/` directory

### Environment Variable Loading
All environment variables are loaded from your `.env` file using the `${env:VAR_NAME}` syntax. This means:
- No sensitive credentials are stored in config files
- Configuration can be shared via version control
- Each developer can use their own `.env` file

### Python Command
The command uses the full path to your conda environment Python interpreter:
- `/opt/anaconda3/envs/cs178/bin/python`
- Update this if your conda environment is at a different location

## Setup Steps

### 1. Create local configuration
```bash
cp .devin/config.json .devin/config.local.json
```

### 2. Load environment variables
```bash
export $(grep -v '^#' .env | xargs)
```

### 3. Verify configuration
```bash
devin mcp list
devin mcp get lockstep
```

## Available MCP Tools

Once configured, these tools are available to Devin:

- `mcp__lockstep__plan_task` - Decompose coding prompts into subtasks with library recommendations
- `mcp__lockstep__resolve_version` - Parse lockfiles and extract pinned versions
- `mcp__lockstep__recommend_library` - Recommend libraries for specific tasks
- `mcp__lockstep__get_versioned_docs` - Fetch version-specific documentation

## Environment Variables Reference

All variables are defined in your `.env` file:

**Required:**
- `REDIS_HOST`, `REDIS_PORT`, `REDIS_USERNAME`, `REDIS_PASSWORD` - Redis connection
- `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` - Supabase connection
- `ANTHROPIC_API_KEY` - Claude API for task decomposition

**Optional (with defaults):**
- `RERANK_MODEL` - Default: `claude-sonnet-4-6`
- `EMBED_MODEL` - Default: `all-MiniLM-L6-v2`
- `EMBED_DIM` - Default: `384`
- `CACHE_CAPACITY` - Default: `1000`
- `CACHE_THETA` - Default: `0.92`
- `DOCS_TTL_SECONDS` - Default: `2592000` (30 days)
- `RECO_TTL_SECONDS` - Default: `900` (15 minutes)
- `DOCS_TOP_K` - Default: `8`
- `LIBS_TOP_K` - Default: `8`
- `LOCKSTEP_HOST` - Default: `127.0.0.1`
- `LOCKSTEP_PORT` - Default: `8000`

## References

- **Step-by-step usage guide**: See [DEVIN_USAGE_GUIDE.md](./DEVIN_USAGE_GUIDE.md)
- **Project overview**: See [README.md](./README.md)
- **Data schema**: See [CONTRACT.md](./CONTRACT.md)
- **Devin CLI MCP docs**: https://devin.ai/docs/extensibility/mcp