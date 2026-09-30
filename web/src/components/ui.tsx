import { useEffect, useRef, useState, type ReactNode } from "react";
import { Icon } from "./Icon";

/** 开关：设置页里所有“开 / 关”都用它，外观统一。 */
export function Switch({ checked, onChange, label, disabled = false }: {
  checked: boolean; onChange: (value: boolean) => void; label: string; disabled?: boolean;
}) {
  return (
    <button type="button" role="switch" aria-checked={checked} aria-label={label} disabled={disabled}
      className={`switch${checked ? " is-on" : ""}`} onClick={() => onChange(!checked)}>
      <span />
    </button>
  );
}

/** 设置项：左边标题和一句说明，右边是开关或按钮。 */
export function SettingRow({ title, description, children }: {
  title: ReactNode; description?: ReactNode; children?: ReactNode;
}) {
  return (
    <div className="setting-row">
      <div className="setting-row-text">
        <strong>{title}</strong>
        {description && <small>{description}</small>}
      </div>
      {children}
    </div>
  );
}

/** 点按钮弹出的小面板；点外面或按 Esc 收起。 */
export function Popover({ label, ariaLabel, children, disabled = false, active = false }: {
  label: ReactNode; ariaLabel: string; children: ReactNode; disabled?: boolean; active?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [place, setPlace] = useState<{ up: boolean; maxHeight: number }>({ up: false, maxHeight: 480 });
  const ref = useRef<HTMLDivElement>(null);
  // 打开前量一下按钮上下各剩多少空间：下方放得下就往下开，否则往空间大的一侧开，并限制高度改为内部滚动。
  function toggle() {
    const rect = ref.current?.getBoundingClientRect();
    if (!open && rect) {
      const below = window.innerHeight - rect.bottom - 16, above = rect.top - 16;
      const up = below < 360 && above > below;
      setPlace({ up, maxHeight: Math.max(160, Math.min(up ? above : below, 560)) });
    }
    setOpen(o => !o);
  }
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDoc); document.removeEventListener("keydown", onKey); };
  }, [open]);
  return (
    <div className="popover" ref={ref}>
      <button type="button" className={`chip-btn${active ? " is-active" : ""}`} aria-label={ariaLabel}
        aria-expanded={open} aria-haspopup="dialog" disabled={disabled} onClick={toggle}>
        {label}
      </button>
      {open && <div className={`popover-panel${place.up ? " is-up" : ""}`} role="dialog" aria-label={ariaLabel}
        style={{ maxHeight: place.maxHeight }}>{children}</div>}
    </div>
  );
}

/** 居中弹窗：点遮罩或按 Esc 关闭。 */
export function Modal({ title, description, onClose, children, footer }: {
  title: string; description?: ReactNode; onClose: () => void; children: ReactNode; footer?: ReactNode;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onMouseDown={e => { if (e.target === e.currentTarget) onClose(); }}>
      <section className="modal" role="dialog" aria-modal="true" aria-label={title}>
        <header className="modal-head">
          <div>
            <h2>{title}</h2>
            {description && <p>{description}</p>}
          </div>
          <button type="button" className="icon-btn" aria-label="关闭" onClick={onClose}><Icon name="close" /></button>
        </header>
        <div className="modal-body">{children}</div>
        {footer && <footer className="modal-foot">{footer}</footer>}
      </section>
    </div>
  );
}

