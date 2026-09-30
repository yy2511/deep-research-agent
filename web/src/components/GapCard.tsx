import { useState, type KeyboardEvent } from "react";
import { Icon } from "./Icon";

/**
 * 开始研究前的澄清卡。
 *
 * 每道题的选项竖着排，单选是圆点、多选是方框；每道题下面都有一个常驻的“补充说明”，
 * 选了选项也能再补一句。每道题都可以跳过。这时还没有生成计划。
 * 点选某道题的选项后，可以按数字键直接切换这道题的选项。
 */

export interface GapOption {
  label: string;
  description?: string;
}

export interface BriefGap {
  id: string;
  question: string;
  header?: string | null;
  options?: GapOption[];
  multi_select?: boolean;
}

export interface GapAnswer {
  id: string;
  selected: string[];
  custom: string;
}

type Draft = { selected: string[]; custom: string };

const COUNT = ["", "一", "两", "三", "四", "五"];

export function GapCard({
  gaps, busy, error, onSubmit, onCancel,
}: {
  gaps: BriefGap[];
  busy: boolean;
  error: string | null;
  onSubmit: (answers: GapAnswer[]) => Promise<void> | void;
  onCancel: () => void;
}) {
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});

  const draftOf = (id: string): Draft => drafts[id] ?? { selected: [], custom: "" };

  function toggle(gap: BriefGap, label: string) {
    setDrafts(all => {
      const current = all[gap.id] ?? { selected: [], custom: "" };
      const selected = gap.multi_select
        ? current.selected.includes(label) ? current.selected.filter((item) => item !== label) : [...current.selected, label]
        : [label];
      return { ...all, [gap.id]: { ...current, selected } };
    });
  }

  function setCustom(gap: BriefGap, custom: string) {
    setDrafts(all => ({ ...all, [gap.id]: { ...(all[gap.id] ?? { selected: [], custom: "" }), custom } }));
  }

  function collect(): GapAnswer[] {
    return gaps.map((gap) => {
      const draft = draftOf(gap.id);
      return { id: gap.id, selected: draft.selected, custom: draft.custom.trim() };
    });
  }

  function numberKey(gap: BriefGap, e: KeyboardEvent<HTMLDivElement>) {
    if (busy || e.target instanceof HTMLInputElement || e.metaKey || e.ctrlKey || e.altKey) return;
    const option = gap.options?.[Number(e.key) - 1];
    if (/^[1-9]$/.test(e.key) && option) { e.preventDefault(); toggle(gap, option.label); }
  }

  return (
    <section className="card prerun-card" aria-label="开始前先确认">
      <div className="prerun-head">
        <h2>开始前，先确认{gaps.length <= 5 ? COUNT[gaps.length] : ` ${gaps.length} `}件事</h2>
        <p>这会影响研究方向。拿不准的可以跳过——跳过的部分会作为假设写进计划，不会被当成事实。</p>
      </div>
      {gaps.map((gap, index) => {
        const draft = draftOf(gap.id);
        const multi = !!gap.multi_select;
        return (
          <div className="gap-question" key={gap.id}>
            <div className="gap-question-title">
              <strong>{index + 1}. {gap.question}</strong>
              {(gap.options?.length ?? 0) > 0 && <small>{multi ? "可多选" : "单选"}</small>}
            </div>
            <div className="gap-options" role="group" aria-label={gap.header ?? gap.question} onKeyDown={e => numberKey(gap, e)}>
              {(gap.options ?? []).map((option, i) => {
                const on = draft.selected.includes(option.label);
                return (
                  <button
                    key={option.label}
                    type="button"
                    className={`gap-option${on ? " is-on" : ""}`}
                    role={multi ? "checkbox" : "radio"}
                    aria-checked={on}
                    disabled={busy}
                    onClick={() => toggle(gap, option.label)}
                  >
                    <span className={`gap-mark ${multi ? "is-box" : "is-dot"}`} aria-hidden="true">
                      {multi && on && <Icon name="check" size={12} strokeWidth={3} />}
                    </span>
                    <span className="gap-option-text">
                      <strong>{option.label}</strong>
                      {option.description && <small>{option.description}</small>}
                    </span>
                    {i < 9 && <kbd>{i + 1}</kbd>}
                  </button>
                );
              })}
              <label className="gap-custom">
                <Icon name="edit" size={15} />
                <input
                  placeholder={(gap.options?.length ?? 0) > 0 ? "补充说明（可选，可以和上面的选项同时填）" : "写下你的回答（可选）"}
                  value={draft.custom}
                  disabled={busy}
                  aria-label={`关于「${gap.header ?? gap.question}」的补充`}
                  onChange={(event) => setCustom(gap, event.target.value)}
                />
              </label>
            </div>
          </div>
        );
      })}
      {error && <p className="field-hint is-bad" role="alert">{error}</p>}
      <div className="card-actions">
        <button className="btn btn-ghost" disabled={busy} onClick={onCancel}>取消</button>
        {gaps.some(g => (g.options?.length ?? 0) > 0) && <span className="field-hint">点选后可按数字键切换</span>}
        <span className="spacer" />
        <button className="btn" disabled={busy} onClick={() => void onSubmit([])}>跳过，直接开始</button>
        <button className="btn btn-primary" disabled={busy} onClick={() => void onSubmit(collect())}>
          {busy ? <><Icon name="loading" className="icon-spin" size={15} />生成计划中…</> : "继续"}
        </button>
      </div>
    </section>
  );
}
