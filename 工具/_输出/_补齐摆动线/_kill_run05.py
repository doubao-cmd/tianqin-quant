import psutil, sys
targets = []
for p in psutil.process_iter(['pid','name','cmdline']):
    try:
        cl = ' '.join(p.info.get('cmdline') or [])
        if 'run_05.py' in cl or '摆动线_回测' in cl:
            targets.append(p)
    except Exception:
        pass
if not targets:
    print("未发现 run_05 / 摆动线_回测 进程"); sys.exit(0)
for p in targets:
    print(f"PID={p.pid} CMD={ ' '.join(p.info.get('cmdline') or [])[:90]}")
    try:
        # 先杀子进程
        for c in p.children(recursive=True):
            try: c.kill(); print(f"  killed child {c.pid}")
            except Exception as e: print(f"  child {c.pid} kill失败 {e}")
        p.kill(); print(f"  killed {p.pid}")
    except Exception as e:
        print(f"  kill {p.pid} 失败: {e}")
print("done")
