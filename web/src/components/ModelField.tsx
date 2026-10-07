import { useState } from "react";
import { Icon } from "./Icon";

// 模型可搜下拉：展开完整列表、输入即过滤、点击选中、仍允许自定义模型名。
export function ModelField({ value, models, onChange, label }: {
  value: string; models: string[]; onChange: (v: string) => void; label: string;
}) {
  const [open, setOpen] = useState(false);
  const q = value.trim().toLowerCase();
  const matches = models.filter((m) => m.toLowerCase().includes(q));
  // 空输入、已等于某个完整模型名、或零匹配（刚切了连接）时展示全部。
  const showAll = q === "" || matches.length === 0 || models.some((m) => m.toLowerCase() === q);
  const filtered = (showAll ? models : matches).slice(0, 60);
  return (
    <div className="model-field">
      <input
        className="input"
        aria-label={label} role="combobox" aria-expanded={open} aria-autocomplete="list"
        value={value}
        placeholder="选择或输入模型名"
        onChange={(e) => { onChange(e.target.value); setOpen(true); }}
        onFocus={() => setOpen(true)}
        onBlur={() => window.setTimeout(() => setOpen(false), 120)}
      />
      {models.length > 0 && (
        <button type="button" className="model-caret" tabIndex={-1} aria-label="展开模型列表"
          onMouseDown={(e) => { e.preventDefault(); setOpen((o) => !o); }}>
          <Icon name="chevron-down" size={14} />
        </button>
      )}
      {open && filtered.length > 0 && (
        <ul className="menu-list" role="listbox" aria-label={`${label}列表`}>
          <li className="menu-list-head">{showAll ? `${models.length} 个模型` : `${filtered.length}/${models.length} 匹配`}</li>
          {filtered.map((m) => (
            // onMouseDown 而非 onClick：抢在 input 的 onBlur 关闭列表之前触发选中
            <li key={m} role="option" aria-selected={m === value} className={`menu-item mono${m === value ? " is-on" : ""}`}
              onMouseDown={() => { onChange(m); setOpen(false); }}>{m}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
