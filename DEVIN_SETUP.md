# Lockstep MCP Server - Devin CLI Setup

This guide explains how to configure the Lockstep MCP server for use with Devin CLI.

## Quick Setup

1. **Copy the example configuration:**
   ```bash
   cp .devin/config.json .devin/config.local.json
   ```

2. **Set your environment variables in `.devin/config.local.json`:**
   Replace `${env:VAR_NAME}` with your actual values or keep them as environment variable references if you have them set in your shell.

3. **Verify your Python path:**
   The example configuration uses `/opt/anaconda3/envs/cs178/bin/python`. Update this if your conda environment is at a different path:
   ```bash
   which python
   # Update the "command" field in .devin/config.local.json with the output
   ```

4. **Test the connection:**
   ```bash
   devin mcp list
   # You should see "lockstep" in the list of available MCP servers
   ```

## Manual Configuration

If you prefer to configure via command line instead of config files:

```bash
devin mcp add lockstep -- /opt/anaconda3/envs/cs178/bin/python -m server
```

Then set your environment variables:

```bash
export REDIS_HOST="your-redis-host"
export REDIS_PORT="your-redis-port"
export REDIS_PASSWORD="your-redis-password"
export SUPABASE_URL="your-supabase-url"
export SUPABASE_KEY="your-supabase-key"
export ANTHROPIC_API_KEY="your-anthropic-api-key"
```

## Available Tools

Once configured, these MCP tools will be available to Devin:

- `mcp__lockstep__plan_task` - Decompose a coding prompt and return a library + docs plan
- `mcp__lockstep__resolve_version` - Parse lockfiles and return pinned dependency versions
- `mcp__lockstep__recommend_library` - Recommend libraries for a coding task
- `mcp__lockstep__get_versioned_docs` - Return documentation chunks for a library at a specific version

## Environment Variables

Required environment variables:

- `REDIS_HOST` - Redis Cloud host
- `REDIS_PORT` - Redis Cloud port
- `REDIS_PASSWORD` - Redis Cloud password
- `SUPABASE_URL` - Supabase project URL
- `SUPABASE_KEY` - Supabase anon/service key
- `ANTHROPIC_API_KEY` - Anthropic API key for Claude-based task decomposition

## Troubleshooting

**Server doesn't start:**
- Verify your Python path is correct: `which python`
- Check that all required dependencies are installed: `pip list`
- Test the server manually: `/opt/anaconda3/envs/cs178/bin/python -m server`

**Connection errors:**
- Verify Redis and Supabase credentials are correct
- Check network connectivity to Redis Cloud and Supabase
- Test Redis connection: `redis-cli -h HOST -p PORT -a PASSWORD ping`

**Tools not appearing:**
- Run `devin mcp list` to verify the server is configured
- Check Devin logs for MCP server startup errors
- Ensure `.devin/config.local.json` is in your project root

## Development

For development, you can run the server in hardcoded mode to test the MCP transport without hitting real backends:

```bash
export LOCKSTEP_HARDCODED=1
devin mcp add lockstep-dev -- /opt/anaconda3/envs/cs178/bin/python -m server
```

## References

- [Devin CLI MCP Documentation](https://devin.ai/docs/extensibility/mcp)
- [Lockstep Project README](./README.md)