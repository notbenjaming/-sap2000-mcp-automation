"""
SAP2000 OAPI 连接测试 v3 - 用 START /B 启动服务
"""
import subprocess
import time
import sys
import os

# 清理
for proc_name in ['SAP2000.exe', 'CSiAPIService.exe']:
    subprocess.run(['taskkill', '/F', '/IM', proc_name, '/T'],
                   capture_output=True, shell=False)
time.sleep(3)

print("=== 启动 CSiAPIService（用 START /B 保留控制台）===\n")

# 用 cmd START /B 启动，保留控制台句柄
cmd_str = 'start /B "" "D:\\SAP2000\\CSiAPIService.exe" -a A -p 11650'
result = subprocess.run(
    ['cmd', '/c', cmd_str],
    capture_output=True, text=True
)
print(f"start 返回: {result.returncode}")

# 等服务启动
time.sleep(5)

# 检查进程
result = subprocess.run(['cmd', '/c', 'tasklist | findstr CSi'],
                       capture_output=True, text=True)
print(f"CSI 进程:\n{result.stdout if result.stdout else '无'}")

# 检查端口
import socket
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(2)
try:
    sock.connect(('127.0.0.1', 11650))
    print(f"\n✅ 端口 11650 可访问（服务在监听）")
    sock.close()
except Exception as e:
    print(f"\n❌ 端口 11650 不可访问: {e}")

print("\n=== 通过 Helper 测试连接 ===\n")

import win32com.client
import pythoncom
import threading

state = {'sap': None, 'model': None, 'error': None,
         'appstart_ret': None, 'appstart_time': 0}

def connect_test():
    try:
        pythoncom.CoInitialize()
        try:
            helper = win32com.client.Dispatch("SAP2000v1.Helper")
            print(f"  ✓ Helper OAPI: {helper.GetOAPIVersionNumber()}", flush=True)

            sap = helper.CreateObjectProgID("CSI.SAP2000.API.SapObject")
            print(f"  ✓ SAP2000 实例已获取", flush=True)
            state['sap'] = sap

            print(f"  → ApplicationStart(5)...", flush=True)
            t1 = time.time()
            ret = sap.ApplicationStart(5)
            state['appstart_time'] = time.time() - t1
            print(f"  ✓ 返回: {ret} ({state['appstart_time']:.1f}s)", flush=True)
            state['appstart_ret'] = ret

            if ret == 0:
                model = sap.SapModel
                state['model'] = model
                ret2 = model.InitializeNewModel()
                print(f"  ✓ InitializeNewModel: {ret2}", flush=True)
        finally:
            pythoncom.CoUninitialize()
    except Exception as e:
        state['error'] = e

t = threading.Thread(target=connect_test, daemon=True)
t.start()

# 监控 SAP2000 进程
for i in range(180):
    t.join(timeout=5)
    if not t.is_alive():
        print(f"\n  ✓ 线程完成 @ {(i+1)*5}s", flush=True)
        break

    if (i+1) % 6 == 0:
        result = subprocess.run(['cmd', '/c', 'tasklist | findstr SAP2000'],
                              capture_output=True, text=True)
        for line in result.stdout.split('\n'):
            if 'SAP2000.exe' in line:
                parts = line.split()
                if len(parts) >= 5:
                    mem_mb = int(parts[-1].replace(',', '')) / 1024
                    print(f"  [{(i+1)*5}s] SAP2000: {mem_mb:.0f} MB", flush=True)
                break

print()
print("=" * 50)
if state['error']:
    print(f"❌ 异常: {state['error']}")
elif state['appstart_ret'] is not None:
    print(f"✅ ApplicationStart: {state['appstart_ret']} ({state['appstart_time']:.1f}s)")
    if state['model']:
        print(f"\n🎉 SAP2000 OAPI 完整可用！")
else:
    print(f"⚠️ 15 分钟超时")
print("=" * 50)

# 清理
subprocess.run(['taskkill', '/F', '/IM', 'SAP2000.exe', '/T'], capture_output=True)
subprocess.run(['taskkill', '/F', '/IM', 'CSiAPIService.exe', '/T'], capture_output=True)
time.sleep(2)
print("\n清理完成")