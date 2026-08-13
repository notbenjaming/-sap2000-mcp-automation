"""
SAP2000 OAPI 连接测试 v2
策略：
1. 启动 CSiAPIService（必须以管理员权限运行 service？）
2. 通过 Helper.CreateObjectProgID 启动 SAP2000
3. 等待 SAP2000 完全加载（可能要几分钟）
4. 测试 ApplicationStart
"""
import subprocess
import time
import sys
import os
import win32com.client
import pythoncom
import threading

# 清理残留
print("清理残留进程...")
for proc_name in ['SAP2000.exe', 'CSiAPIService.exe']:
    subprocess.run(['taskkill', '/F', '/IM', proc_name, '/T'],
                   capture_output=True, shell=False)
time.sleep(3)

# 启动 CSiAPIService
print("\n启动 CSiAPIService...")
service_log = open(r"D:\SAP2000\service_log.txt", "w")
service = subprocess.Popen(
    [r"D:\SAP2000\CSiAPIService.exe", "-a", "A", "-p", "11650"],
    stdout=service_log, stderr=subprocess.STDOUT,
    creationflags=subprocess.DETACHED_PROCESS
)
print(f"服务 PID: {service.pid}")
time.sleep(3)

# 检查服务是否活着
poll = service.poll()
if poll is not None:
    print(f"❌ 服务已退出 (code={poll})")
    with open(r"D:\SAP2000\service_log.txt", "r") as f:
        print(f.read())
    sys.exit(1)
print("✓ 服务运行中")

# 通过 Helper 连接
print("\n通过 Helper 连接 SAP2000...")
print("（首次启动 SAP2000 可能需要 4-5 分钟）\n")

state = {'sap': None, 'model': None, 'error': None,
         'appstart_ret': None, 'appstart_time': 0,
         'log': []}

def log(msg):
    state['log'].append(msg)
    print(msg, flush=True)

def connect_test():
    try:
        pythoncom.CoInitialize()
        try:
            log("[1/4] 创建 Helper...")
            helper = win32com.client.Dispatch("SAP2000v1.Helper")

            ver = helper.GetOAPIVersionNumber()
            log(f"      OAPI 版本: {ver}")

            log("[2/4] CreateObjectProgID(CSI.SAP2000.API.SapObject)...")
            t0 = time.time()
            sap = helper.CreateObjectProgID("CSI.SAP2000.API.SapObject")
            log(f"      SAP2000 实例已获取 ({time.time()-t0:.1f}s)")
            state['sap'] = sap

            log("[3/4] ApplicationStart(5)...")
            t1 = time.time()
            ret = sap.ApplicationStart(5)
            state['appstart_time'] = time.time() - t1
            log(f"      返回: {ret} ({state['appstart_time']:.1f}s)")
            state['appstart_ret'] = ret

            if ret == 0:
                log("[4/4] 获取 SapModel + InitializeNewModel...")
                model = sap.SapModel
                state['model'] = model
                ret2 = model.InitializeNewModel()
                log(f"      InitializeNewModel 返回: {ret2}")
                log("\n" + "=" * 50)
                log("✅ 真实 SAP2000 OAPI 完全可用！")
                log("=" * 50)
        finally:
            pythoncom.CoUninitialize()
    except Exception as e:
        state['error'] = e
        log(f"❌ 异常: {e}")

t = threading.Thread(target=connect_test, daemon=True)
t.start()

# 等待 + 监控 SAP2000 进程
print("监控 SAP2000 进程 + 等待线程完成...")
for i in range(180):  # 15 分钟
    t.join(timeout=5)
    if not t.is_alive():
        print(f"\n[完成 @ {(i+1)*5}s]")
        break

    # 每 30 秒打印 SAP2000 状态
    if (i+1) % 6 == 0:
        result = subprocess.run(['cmd', '/c', 'tasklist | findstr SAP2000'],
                              capture_output=True, text=True)
        for line in result.stdout.split('\n'):
            if 'SAP2000.exe' in line:
                parts = line.split()
                if len(parts) >= 5:
                    mem_mb = int(parts[-1].replace(',', '')) / 1024
                    print(f"  [{(i+1)*5}s] SAP2000 内存: {mem_mb:.0f} MB")
                break

# 最终结果
print("\n" + "=" * 50)
print("最终结果")
print("=" * 50)

if state['error']:
    print(f"❌ 异常: {state['error']}")
elif state['appstart_ret'] is not None:
    print(f"✅ ApplicationStart 返回: {state['appstart_ret']} ({state['appstart_time']:.1f}s)")
    if state['model']:
        print("✅ SapModel + InitializeNewModel 成功")
        print("\n🎉 SAP2000 OAPI 完全可用！")
else:
    print(f"⚠️ 线程超时")

# 清理
print("\n清理进程...")
subprocess.run(['taskkill', '/F', '/IM', 'SAP2000.exe', '/T'], capture_output=True)
subprocess.run(['taskkill', '/F', '/PID', str(service.pid)], capture_output=True)
time.sleep(2)
service_log.close()

print("\n测试结束")