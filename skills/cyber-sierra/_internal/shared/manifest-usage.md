# Manifest Usage

The CLI manifest is the single source of truth for available commands. No component may hardcode CLI knowledge. This document defines how to query and interpret the manifest.

## CLI Environment

The `cybersierra` CLI is distributed as the npm package `@cybersierra/cybersierra-cli`.

Before running any `cybersierra` command, verify it is installed:

```bash
which cybersierra || npm install -g @cybersierra/cybersierra-cli
```

If `cybersierra` is not found, install it globally before proceeding. After installation it will be available on PATH.

## Querying the Manifest

### Full catalog (all modules)

```bash
cybersierra manifest
```

Returns:

```json
{
  "data": [ CatalogEntry, ... ],
  "meta": { "version": "string", "count": number }
}
```

### Raw tree (module-keyed)

```bash
cybersierra manifest --raw
```

Returns:

```json
{
  "version": "string",
  "tree": { "module": [ Operation, ... ], ... }
}
```

### Filtering to specific modules

Use `--raw` and filter client-side:

```bash
cybersierra manifest --raw | python3 -c "
import json, sys
tree = json.load(sys.stdin)['tree']
for m in ['MODULE_1', 'MODULE_2']:
    if m in tree:
        print(json.dumps({m: tree[m]}, indent=2))
"
```

The Router determines which modules are relevant. The Planner receives only those modules.

## CatalogEntry Schema

```typescript
CatalogEntry {
  command: string        // "module resource action"
  module: string
  resource: string
  action: string
  method: string         // GET | POST | PUT | PATCH | DELETE
  path: string           // OpenAPI path
  description?: string
  safe: boolean          // true = read-only, false = write operation
  params: Param[]
}

Param {
  name: string
  in: "path" | "query" | "body" | "header" | "file"
  required: boolean
  default?: any
  enum?: any[]
  examples?: any[]
}
```

## Constructing CLI Commands

```
cybersierra <module> <resource> <action> [flags]
```

| Param location | CLI flag                             |
| -------------- | ------------------------------------ |
| `path`, `query`| `--paramName value`                  |
| `body`         | `--data '{"key": "value"}'`          |
| `file`         | `--file /path/to/file`               |

## Safety

- `safe: true` commands are read-only. Execute freely.
- `safe: false` commands modify state. Require user confirmation before execution.

## Module Discovery

Do not hardcode module lists. Query `cybersierra manifest --raw` and inspect the `tree` keys to discover available modules dynamically.
