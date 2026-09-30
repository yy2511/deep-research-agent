/** 规划进度的状态与合并规则（不含界面，App 的处理逻辑和进度框共用）。 */
import type { PlannerEvent } from "./api";

export type PlanStage = "gaps" | "plan";

/** 规划进行中的状态：要走哪几步、现在到哪一步、各步开始时间、各步的思考内容。 */
export interface PlanProgress {
  steps: PlanStage[];
  stage: PlanStage | null;
  startedAt: number;
  stageStartedAt: number;
  thinking: Partial<Record<PlanStage, string>>;
}

export const STEP_LABEL: Record<PlanStage, string> = {
  gaps: "判断要不要先问你几个问题",
  plan: "生成研究计划",
};

export function startProgress(steps: PlanStage[], now = Date.now()): PlanProgress {
  return { steps, stage: null, startedAt: now, stageStartedAt: now, thinking: {} };
}

/** 把服务端推来的一条消息合进进度状态。 */
export function applyPlannerEvent(progress: PlanProgress, event: PlannerEvent, now = Date.now()): PlanProgress {
  if (event.type === "stage") {
    return { ...progress, stage: event.stage, stageStartedAt: now };
  }
  const previous = progress.thinking[event.stage] ?? "";
  return { ...progress, thinking: { ...progress.thinking, [event.stage]: previous + event.text } };
}

/** 各步思考内容拼成一段，供规划结束后回看。 */
export function thinkingText(progress: PlanProgress | null): string {
  if (!progress) return "";
  return progress.steps
    .filter((step) => progress.thinking[step])
    .map((step) => (progress.steps.length > 1 ? `【${STEP_LABEL[step]}】\n` : "") + progress.thinking[step])
    .join("\n\n");
}
