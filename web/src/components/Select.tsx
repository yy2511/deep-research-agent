import { useEffect, useRef, useState } from "react";
import { Icon } from "./Icon";

export interface SelectOption { value: string; label: string; disabled?: boolean; hint?: string }

// 自绘下拉：原生 <select> 展开的是操作系统菜单，CSS 管不到（灰底系统样式）。
// 换成受控的按钮 + 绝对定位列表，跟 SettingsModal 的模型可搜下拉同一套观感。
export function Select({ value, options, onChange, disabled, ariaLabel }: {
  value: string;
  options: SelectOption[];
  onChange: (v: string) => void;
  disabled?: boolean;
  ariaLabel?: string;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const current = options.find((o) => o.value === value);

  // 点外部 / 按 Esc 收起
  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) { if (e.key === "Escape") setOpen(false); }
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDoc); document.removeEventListener("keydown", onKey); };
  }, [open]);

  return (
    <div className={`uiselect${open ? " open" : ""}`} ref={ref}>
      <button
        type="button" className="uiselect-btn" disabled={disabled} aria-label={ariaLabel}
        aria-haspopup="listbox" aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="uiselect-val">{current?.label ?? value}</span>
        <Icon name="chevron-down" className="uiselect-caret" size={14} />
      </button>
      {open && !disabled && (
        <ul className="uiselect-list" role="listbox">
          {options.map((o) => (
            <li
              key={o.value} role="option" aria-selected={o.value === value}
              className={`uiselect-opt${o.value === value ? " on" : ""}${o.disabled ? " disabled" : ""}`}
              onClick={() => { if (o.disabled) return; onChange(o.value); setOpen(false); }}
            >
              <span className="uiselect-check" aria-hidden>
                {o.value === value && <Icon name="check" size={13} />}
              </span>
              <span className="uiselect-opt-label">{o.label}</span>
              {o.hint && <span className="uiselect-opt-hint">{o.hint}</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
