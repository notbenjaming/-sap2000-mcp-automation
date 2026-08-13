"""
诊断：列出所有 SAP2000 相关进程
"""
import subprocess

result = subprocess.run(['cmd', '/c', 'tasklist | findstr -i "sap"'],
                       capture_output=True, text=True, shell=True)
print("=== 当前 SAP2000 相关进程 ===\n")
print(result.stdout if result.stdout else "无")

# 看完整 tasklist
result = subprocess.run(['cmd', '/c', 'tasklist'],
                       capture_output=True, text=True, shell=True)
lines = result.stdout.split('\n')
sap_lines = [l for l in lines if 'SAP' in l.upper() or 'CSI' in l.upper()]
print("=== 所有 SAP/CSI 进程 ===")
for line in sap_lines:
    print(line)