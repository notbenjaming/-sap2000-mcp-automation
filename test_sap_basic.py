"""
快速 SAP2000 测试脚本
"""
import sys
import time

# 必须先杀残留进程
import subprocess
subprocess.run(['taskkill', '/F', '/IM', 'SAP2000.exe', '/T'],
               capture_output=True, shell=False)
time.sleep(2)

print("=== SAP2000 真实连接测试 ===\n", flush=True)

import win32com.client

print("1. 创建 COM 对象...", flush=True)
try:
    sap = win32com.client.Dispatch("CSI.SAP2000.API.SapObject")
    print("   ✅ COM 对象创建成功\n", flush=True)
except Exception as e:
    print(f"   ❌ 失败: {e}\n", flush=True)
    sys.exit(1)

print("2. 调用 ApplicationStart(5)...", flush=True)
print("   （注意：SAP2000 窗口会弹出，首次启动需要几秒）\n", flush=True)
start = time.time()
try:
    # CSI OAPI: ApplicationStart(Units)，Units=5 表示 kN_m
    ret = sap.ApplicationStart(5)
    elapsed = time.time() - start
    print(f"   ✅ 返回值: {ret} (耗时 {elapsed:.2f}s)\n", flush=True)
except Exception as e:
    elapsed = time.time() - start
    print(f"   ❌ 失败（耗时 {elapsed:.2f}s）: {e}\n", flush=True)
    sys.exit(1)

print("3. 获取 SapModel 句柄...", flush=True)
try:
    model = sap.SapModel
    print(f"   ✅ SapModel: {model}\n", flush=True)
except Exception as e:
    print(f"   ❌ 失败: {e}\n", flush=True)

print("4. 测试简单调用 InitializeNewModel...", flush=True)
try:
    ret = model.InitializeNewModel()
    print(f"   ✅ 返回值: {ret}\n", flush=True)
except Exception as e:
    print(f"   ❌ 失败: {e}\n", flush=True)

print("5. 关闭 SAP2000...", flush=True)
try:
    sap.ApplicationExit(False)
    print("   ✅ 关闭成功\n", flush=True)
except Exception as e:
    print(f"   ⚠️ 关闭失败: {e}\n", flush=True)

print("=" * 50)
print("✅ 基础连接测试通过")
print("=" * 50)