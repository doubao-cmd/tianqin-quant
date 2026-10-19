// localStorage 命名空间存储（P2-2/P2-3 共用）
//
// 为什么抽这一层：marks.js 与 ledger.js 原本各写一份
//   let _mem = null, _loaded = false; function _load(){...} function _save(){...}
// 拼成单文件 bundle 时（build_html.py 去掉 import/export 后首尾相接，全部模块同一个作用域），
// 顶层 const/let 重名直接 SyntaxError：`Identifier 'LS_KEY' has already been declared`
// （P2-3 实测踩到，一次报 4 组：LS_KEY / _mem / _load / _save）。
// ⇒ 抽成工厂函数，内部状态关在闭包里，顶层只暴露 lsStore 一个名字。
//
// 语义（两个使用方一致，别私自改）：
//   · 一次页面生命周期只读盘一次；之后 save 同步写回落盘
//   · localStorage 不可用（隐私模式/配额满/file:// 被禁）⇒ 降级内存，刷新即丢，并 toast 明说
import { toast } from './fmt.js';

export function lsStore(key, opts = {}) {
  const cap = opts.cap || 0;                     // 软封顶（0 = 不封）；超出砍最旧
  const clean = opts.clean || (x => x);          // 读盘时逐条清洗/过滤
  let mem = null, loaded = false;

  function load() {
    if (loaded) return mem;
    loaded = true;
    mem = [];
    try {
      const raw = localStorage.getItem(key);
      if (raw) {
        const a = JSON.parse(raw);
        if (Array.isArray(a)) mem = a.map(clean).filter(Boolean);
      }
    } catch (e) { /* 读不到就当空，走内存降级 */ }
    return mem;
  }

  function save(list) {
    let out = list;
    if (cap && out.length > cap) {
      out = out.slice(out.length - cap);
      if (opts.onCap) opts.onCap(cap);
    }
    mem = out;
    loaded = true;
    try {
      localStorage.setItem(key, JSON.stringify(out));
    } catch (e) {
      toast(opts.msg || 'localStorage 不可用：数据只在本次会话有效');
    }
    return out;
  }

  return { load, save };
}
