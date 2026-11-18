"""A tiny stdio MCP server for tests: tools echo and boom."""
import json
import sys

for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    m, i = msg["method"], msg["id"]
    if m == "initialize":
        res = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}
    elif m == "tools/list":
        res = {"tools": [
            {"name": "echo", "description": "Echo text", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
            {"name": "boom", "description": "Always errors", "inputSchema": {"type": "object", "properties": {}}}]}
    elif m == "tools/call":
        p = msg["params"]
        if p["name"] == "echo":
            res = {"content": [{"type": "text", "text": "echo:" + p["arguments"]["text"]}]}
        else:
            res = {"content": [{"type": "text", "text": "kaput"}], "isError": True}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": i, "error": {"code": -32601, "message": "nope"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": i, "result": res}), flush=True)
