#!/usr/bin/env python3
"""MCP Server 验证脚本 — 启动 mcp_server.py，发送一组工具调用，验证返回

用法：python test_mcp_client.py
"""

import subprocess
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.resolve()


def main():
    # 启动 mcp_server
    proc = subprocess.Popen(
        [sys.executable, "mcp_server.py"],
        cwd=str(PROJECT_ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    def send(msg):
        line = json.dumps(msg) + "\n"
        proc.stdin.write(line)
        proc.stdin.flush()
        print(f"  → {line.strip()[:120]}")

    import threading
    response_queue = []
    response_lock = threading.Lock()

    def reader_thread():
        for line in proc.stdout:
            line = line.strip()
            if line:
                try:
                    with response_lock:
                        response_queue.append(json.loads(line))
                except Exception as e:
                    print(f"  parse err: {e}: {line[:200]}")

    reader = threading.Thread(target=reader_thread, daemon=True)
    reader.start()

    def recv(timeout=60):
        import time
        start = time.time()
        while time.time() - start < timeout:
            with response_lock:
                if response_queue:
                    return response_queue.pop(0)
            time.sleep(0.1)
        return {"error": "timeout"}

    def call_tool(name, args=None, msg_id=100):
        send({
            "jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
            "params": {"name": name, "arguments": args or {}}
        })
        return recv()

    print("=" * 60)
    print("MCP Server 验证")
    print("=" * 60)

    # 1. initialize
    print("\n[1] initialize")
    send({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0"}
        }
    })
    r = recv()
    print(f"  ← {json.dumps(r, ensure_ascii=False)[:300]}")

    # 2. initialized notification
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    # 3. list tools
    print("\n[2] tools/list")
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    r = recv()
    if r and "result" in r:
        tools = r["result"].get("tools", [])
        print(f"  ← 共 {len(tools)} 个工具:")
        for t in tools:
            print(f"     - {t['name']}: {t.get('description', '')[:60]}")

    # 4. sap2000_status
    print("\n[3] tools/call sap2000_status")
    r = call_tool("sap2000_status", msg_id=3)
    if r and "result" in r:
        content = r["result"].get("content", [])
        for c in content:
            print(f"  ← {c.get('text', '')[:300]}")

    # 5. show_model
    print("\n[4] tools/call show_model")
    r = call_tool("show_model", msg_id=4)
    if r and "result" in r:
        content = r["result"].get("content", [])
        for c in content:
            print(f"  ← {c.get('text', '')[:300]}")

    # 6. show_forces（如果 SAP2000 跑过分析）
    print("\n[5] tools/call show_forces")
    r = call_tool("show_forces", msg_id=5)
    if r and "result" in r:
        content = r["result"].get("content", [])
        for c in content:
            print(f"  ← {c.get('text', '')[:500]}")

    # 7. modify
    print("\n[6] tools/call modify (改梁5荷载为-30)")
    r = call_tool("modify", {"command": "改梁5荷载为-30"}, msg_id=6)
    if r and "result" in r:
        content = r["result"].get("content", [])
        for c in content:
            print(f"  ← {c.get('text', '')[:500]}")

    # 8. solve
    print("\n[7] tools/call solve")
    r = call_tool("solve", msg_id=7)
    if r and "result" in r:
        content = r["result"].get("content", [])
        for c in content:
            print(f"  ← {c.get('text', '')[:800]}")

    # 收尾
    proc.stdin.close()
    proc.terminate()
    print("\n" + "=" * 60)
    print("验证完成")
    print("=" * 60)


if __name__ == "__main__":
    main()
