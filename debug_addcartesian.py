"""
深入诊断 SAP2000 AddCartesian ret=1 问题
"""
import subprocess
import time
import win32com.client
import pythoncom
import threading

# 清理
for proc_name in ['SAP2000.exe']:
    subprocess.run(['taskkill', '/F', '/IM', proc_name, '/T'],
                   capture_output=True, shell=False)
time.sleep(2)

def log(msg):
    print(msg, flush=True)

state = {'log': [], 'error': None}

def test():
    try:
        pythoncom.CoInitialize()
        try:
            helper = win32com.client.Dispatch("SAP2000v1.Helper")
            sap = helper.CreateObjectProgID("CSI.SAP2000.API.SapObject")
            log(f"✓ SAP2000 已连接")

            sap.ApplicationStart(5)
            model = sap.SapModel
            model.InitializeNewModel()
            model.File.NewBlank()
            log(f"✓ 模型初始化")

            # 看 PointObj 的方法
            log("\n=== PointObj 方法列表 ===")
            point_methods = [m for m in dir(model.PointObj) if 'Add' in m or 'Cart' in m]
            for m in point_methods[:15]:
                log(f"  - {m}")

            # 看 AddCartesian 的 docstring / help
            log("\n=== AddCartesian 帮助信息 ===")
            try:
                help_text = model.PointObj.AddCartesian.__doc__
                log(f"  doc: {help_text}")
            except Exception as e:
                log(f"  无 doc: {e}")

            # 测试不同参数组合
            log("\n=== 测试不同的 AddCartesian 参数 ===")

            # 测试 A: 最少参数
            try:
                ret = model.PointObj.AddCartesian(0, 0, 0, "")
                log(f"  A: (0,0,0,'') = {ret}")
            except Exception as e:
                log(f"  A: 异常 {e}")

            # 测试 B: 显式命名参数（Python 风格）
            try:
                ret = model.PointObj.AddCartesian(
                    0, 0, 0, "", "GLOBAL", True, 0
                )
                log(f"  B: CSys='GLOBAL' = {ret}")
            except Exception as e:
                log(f"  B: 异常 {e}")

            # 测试 C: 不传 CSys（让它默认）
            try:
                # 调用顺序: AddCartesian(X, Y, Z, Name)
                # 不传可选参数
                ret = model.PointObj.AddCartesian(0.0, 0.0, 0.0, "TEST1")
                log(f"  C: 只传 4 参数 = {ret}")
            except Exception as e:
                log(f"  C: 异常 {e}")

            # 测试 D: 用坐标稍有不同的节点
            try:
                ret = model.PointObj.AddCartesian(1.0, 0.0, 0.0, "TEST2")
                log(f"  D: (1,0,0,'TEST2') = {ret}")
            except Exception as e:
                log(f"  D: 异常 {e}")

        finally:
            pythoncom.CoUninitialize()
    except Exception as e:
        state['error'] = e

t = threading.Thread(target=test, daemon=True)
t.start()
t.join(timeout=120)

if state['error']:
    print(f"\n❌ 总异常: {state['error']}")

subprocess.run(['taskkill', '/F', '/IM', 'SAP2000.exe', '/T'], capture_output=True)