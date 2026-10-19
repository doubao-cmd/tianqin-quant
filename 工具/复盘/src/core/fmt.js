// 通用格式化与小工具（无副作用，纯函数）
export const $ = id => document.getElementById(id);

export const esc = s => String(s == null ? '' : s).replace(/[&<>]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

export const f1 = v => v == null ? '—' : Number(v).toFixed(1);
export const f2 = v => v == null ? '—' : Number(v).toFixed(2);
export const sgn = v => v == null ? '—' : (v > 0 ? '+' : '') + Number(v).toFixed(0);
export const num0 = v => (v == null || !isFinite(v)) ? '—'
  : Number(v).toLocaleString('en-US', {maximumFractionDigits: 0});
export const num2 = v => (v == null || !isFinite(v)) ? '—' : Number(v).toFixed(2);
export const money = v => (v == null || !isFinite(v)) ? '—'
  : (v > 0 ? '+' : '') + Number(v).toLocaleString('en-US', {maximumFractionDigits: 0});
export const pct = (n, d) => d ? Math.round(n / d * 100) + '%（' + n + '/' + d + '）' : '—';

// 轻量 toast（P1-5 用于"训练模式禁用"等提示）
let _toastEl = null, _toastT = null;
export function toast(msg) {
  if (!_toastEl) {
    _toastEl = document.createElement('div');
    _toastEl.id = '_toast';
    _toastEl.style.cssText = 'position:fixed;left:50%;top:14px;transform:translateX(-50%);'
      + 'background:#1a1d26;border:1px solid #2a2f3a;color:#e6e6e6;padding:6px 12px;'
      + 'border-radius:6px;font-size:12px;z-index:9999;box-shadow:0 4px 16px rgba(0,0,0,.5);'
      + 'transition:opacity .3s;pointer-events:none';
    document.body.appendChild(_toastEl);
  }
  _toastEl.textContent = msg;
  _toastEl.style.opacity = '1';
  if (_toastT) clearTimeout(_toastT);
  _toastT = setTimeout(() => { if (_toastEl) _toastEl.style.opacity = '0'; }, 1800);
}
